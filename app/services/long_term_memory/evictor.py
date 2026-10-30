# app/services/long_term_memory/evictor.py
"""淘汰路径 · 分类型衰减 + 淘汰日志（E11 实装；低频触发）。

规则（D26 定稿；零 LLM、确定性纯函数——同输入同输出，评测可复现）：
- identity / relation：豁免（身份与关系是长期骨架，永不衰减）；
- goal：按 event_time 确定性过期——event_time + EVICT_GOAL_GRACE_DAYS < now → 下架
  （无 event_time 的目标信息不足，不判）；
- preference：慢衰减 score = 0.5^(age_days / HL)，HL = EVICT_PREF_HALFLIFE_DAYS
  （source=explicit 时 ×2 放宽——用户明确要记的衰减更慢）；score < EVICT_MIN_SCORE → 下架；
  age 基准 = now - (last_accessed_at or created_at)——被读到就续命；
- event：V1 不衰减（历史是证据，不随时间失效）；
- 非 active（superseded / revoked / evicted）：本就不在扫描面（sweep 只扫 active）。

触发：启动巡检（main.py lifespan）/ 面板手动（POST /memory/evict）/ 评测调用。
不做常驻定时器（范围守卫：单用户交互式场景，启动 + 手动足够；定时器是部署形态问题）。

执行收口（与 D5 写序列一致）：evict_cards（只 flush）→ commit_and_sync
（delete_card_ids=被下架卡）——非 active 自动走删除路径（快表摘除 + Qdrant 删向量），
DB / 快表 / 向量三面一次一致。

日志：EVICT_LOG_PATH（JSONL，fail-open——写失败只告警，淘汰结果不回退）；每条含
card_id / kind / subtype / rule / reason / score / evidence——面板"淘汰日志"的数据源，
也是"记忆不越界 / 可归因"的证据。
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import logger
from app.models.domain import Card
from app.services.long_term_memory import store

# 豁免集合（永不衰减的 entity 子类；与 domain.CARD_SUBTYPES_ENTITY 对齐）
_EXEMPT_SUBTYPES = ("identity", "relation")


def _ensure_utc(dt: datetime | None) -> datetime | None:
    """时区归一：naive 视为 UTC（SQLite 读回值），aware 一律转 UTC（对齐 pool._as_utc）。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _pref_score(card: Card, now: datetime) -> tuple[float, datetime]:
    """偏好卡衰减分：score = 0.5^(age_days / HL)；explicit 半衰期 ×2（放宽）。

    age 基准 = last_accessed_at or created_at（被访问续命）；返回 (score, 基准时刻)。
    """
    base = _ensure_utc(card.last_accessed_at) or _ensure_utc(card.created_at) or now
    age_days = max((now - base).total_seconds(), 0.0) / 86400.0
    halflife = settings.EVICT_PREF_HALFLIFE_DAYS * (2 if card.source == "explicit" else 1)
    return 0.5 ** (age_days / max(halflife, 1.0)), base


def _judge(card: Card, now: datetime) -> dict[str, Any] | None:
    """单卡判定（纯函数；时钟注入）：返回下架理由 dict；保留 → None。"""
    if card.kind == "entity":
        subtype = (card.subtype or "").lower()
        if subtype in _EXEMPT_SUBTYPES:
            return None
        if subtype == "goal":
            event_time = _ensure_utc(card.event_time)
            if event_time is None:
                return None                       # 无时间的目标信息不足，不判
            deadline = event_time + timedelta(days=settings.EVICT_GOAL_GRACE_DAYS)
            if deadline < now:
                return {
                    "rule": "goal_expired",
                    "reason": (f"目标过期：event_time {event_time.date()} + 宽限 "
                               f"{settings.EVICT_GOAL_GRACE_DAYS} 天 < {now.date()}"),
                    "score": 0.0,
                    "evidence": {"event_time": event_time.isoformat(),
                                 "grace_days": settings.EVICT_GOAL_GRACE_DAYS},
                }
            return None
        if subtype == "preference":
            score, base = _pref_score(card, now)
            if score < settings.EVICT_MIN_SCORE:
                halflife = settings.EVICT_PREF_HALFLIFE_DAYS * (
                    2 if card.source == "explicit" else 1)
                return {
                    "rule": "preference_decay",
                    "reason": (f"偏好衰减：score={score:.3f} < 阈值 "
                               f"{settings.EVICT_MIN_SCORE}（半衰期 {halflife} 天）"),
                    "score": round(score, 4),
                    "evidence": {"base_time": base.isoformat(),
                                 "halflife_days": halflife,
                                 "source": card.source},
                }
            return None
        return None                               # 词表外子类（防未来扩展误伤）
    return None      # event：V1 不衰减；task：占位不参与


async def _list_all_active(session: AsyncSession) -> list[Card]:
    """游标分页拉全量 active 卡（id 升序；不随并发写入漂移）。"""
    result: list[Card] = []
    after_id: int | None = None
    while True:
        page = await store.list_cards(session, after_id=after_id, limit=500)
        result.extend(page)
        if len(page) < 500:
            return result
        after_id = page[-1].id


def _log_path() -> str:
    """淘汰日志路径（相对路径基于 settings.BASE_DIR）。"""
    p = settings.EVICT_LOG_PATH
    return p if os.path.isabs(p) else os.path.join(settings.BASE_DIR, p)


def _append_log(records: list[dict[str, Any]]) -> None:
    """追加淘汰日志（fail-open：写失败只告警——日志可丢，淘汰不回退）。"""
    if not records:
        return
    try:
        path = _log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            for record in records:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001 —— fail-open 是本模块的硬约定
        logger.warning(f"淘汰日志写盘失败（已忽略，淘汰结果不受影响）: {exc}")


def read_evict_log(limit: int = 50) -> list[dict[str, Any]]:
    """读最近 N 条淘汰日志（面板用；最新在前——倒序返回）。文件不存在 → []。"""
    path = _log_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except Exception as exc:  # noqa: BLE001 —— 读失败不阻塞面板
        logger.warning(f"淘汰日志读取失败（返回空）: {exc}")
        return []
    records: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:              # 坏行跳过（审计日志容忍残缺）
            continue
    return list(reversed(records))


async def sweep(session: AsyncSession, *, now: datetime | None = None) -> dict[str, Any]:
    """全量巡检一次：判定 → 批量下架 → 三面同步 → 淘汰日志；返回报告。

    报告：{"scanned": 扫描数, "evicted": 下架数, "details": [每卡理由],
           "stage_ms": {"scan"/"judge"/"apply"}}。

    时钟注入 now（默认 UTC 当前）——测试可传固定时钟复现衰减判定（确定性纪律）。
    会话由调用方持有（端点为请求会话 / lifespan 用会话工厂）；本函数内部提交一次。
    """
    now = _ensure_utc(now) or datetime.now(timezone.utc)
    t0 = time.perf_counter()

    cards = await _list_all_active(session)              # ① 扫描（只读）
    t1 = time.perf_counter()
    hits: list[tuple[Card, dict[str, Any]]] = []         # ② 判定（纯规则，零 IO）
    for card in cards:
        verdict = _judge(card, now)
        if verdict is not None:
            hits.append((card, verdict))
    t2 = time.perf_counter()

    evicted_ids: list[int] = []                          # ③ 执行（唯一提交点）
    if hits:
        evicted_ids = await store.evict_cards(session, [c.id for c, _ in hits])
        await store.commit_and_sync(session, delete_card_ids=evicted_ids)
    t3 = time.perf_counter()

    ts = now.isoformat()
    _append_log([
        {"ts": ts, "card_id": card.id, "kind": card.kind, "subtype": card.subtype,
         "content": card.content[:80], "rule": verdict["rule"],
         "reason": verdict["reason"], "score": verdict["score"],
         "evidence": verdict["evidence"]}
        for card, verdict in hits
    ])

    report = {
        "scanned": len(cards),
        "evicted": len(evicted_ids),
        "details": [
            {"card_id": card.id, "kind": card.kind, "subtype": card.subtype,
             "rule": verdict["rule"], "reason": verdict["reason"],
             "score": verdict["score"]}
            for card, verdict in hits
        ],
        "stage_ms": {"scan": round((t1 - t0) * 1000, 1),
                     "judge": round((t2 - t1) * 1000, 1),
                     "apply": round((t3 - t2) * 1000, 1)},
    }
    logger.info(f"淘汰巡检完成：扫描 {report['scanned']} 张，下架 {report['evicted']} 张"
                f"（{report['stage_ms']}）")
    return report
