# app/services/long_term_memory/explicit.py
"""写路径 · 显式指令通道（E8）：识别“记住 X” → Redis 零延迟暂存 → 后台单独优先抽取。

设计（协商定案，D22）：
- 用户说“记住 X” → **不建“原话卡”入库**（原话会污染规范卡格式：卡片是有损抽象、原文在 messages），
  而是把这条消息**优先单独抽取**（不等攒批），产卡打 source="explicit" 标签；
- 零延迟面 = Redis List（chat:explicit_recent）：命中即暂存，后台任务马上处理；
  “用户刚说完马上追问”的场景由滑动窗口（short_term_memory）+ 本条暂存共同兜住；
- 不塞快表：快表只装规范卡（E6 契约），原话不进快表；
- 读时优待（消费契约，E10/E11 落地）：source="explicit" 的卡在精筛加权 + 同分优先、
  淘汰放宽——用户主动要记 = 重要（写时不判重要性、读时优待）；
- 降级双保险：识别/暂存只影响“零延迟”档待遇——消息已正常归档，批抽取（水位通道）照样覆盖；
  抽取失败保留缓存（下轮重试）；长期未处理由批抽取扫到后兜底清账（consolidator 协调）。

Redis 归属：复用 short_term_memory.redis_client（同连接池）；模块内 `_redis` 别名便于测试替换
（monkeypatch.setattr(explicit, "_redis", fake)）。
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable

from app.core import short_term_memory
from app.core.logging import logger

# 显式指令识别（宁宽勿窄：识别失败无代价——消息已归档、批抽取照样会抽；
# 识别成功则获得“零延迟 + 单独抽取 + explicit 标签”三档优待）
_EXPLICIT_RE = re.compile(r"记住|记一下|记下来|记下|帮我记|记着|别忘了|不要忘记|别忘记|提醒我")

CACHE_KEY = "chat:explicit_recent"   # Redis List：待优先抽取的显式消息（JSON 条目）
CACHE_MAX = 10                        # 条数上限（与滑动窗口同级规模；超出裁旧）
CACHE_TTL_SECONDS = 24 * 3600         # 24h 过期（长期未处理的由批抽取兜底，缓存不无限存）

_redis = short_term_memory.redis_client


def is_explicit(text: str) -> bool:
    """是否显式“记住 X”指令（正则识别；识别失败无代价，见模块头）。"""
    return bool(_EXPLICIT_RE.search(text or ""))


async def push_explicit(*, session_id: str, message_id: int, content: str) -> bool:
    """暂存一条待优先抽取的显式消息（返回是否成功；Redis 故障不抛、返回 False）。

    失败降级：仅失去“零延迟”档待遇——消息已正常归档，批抽取仍会覆盖（双保险）。
    """
    entry = json.dumps({"message_id": message_id, "session_id": session_id,
                        "content": content or ""}, ensure_ascii=False)
    try:
        await _redis.rpush(CACHE_KEY, entry)
        await _redis.ltrim(CACHE_KEY, -CACHE_MAX, -1)
        await _redis.expire(CACHE_KEY, CACHE_TTL_SECONDS)
        return True
    except Exception as e:  # noqa: BLE001 —— 缓存是尽力而为的加速器，不阻断主链路
        logger.warning(f"显式消息暂存失败（降级为批抽取兜底）: {e}")
        return False


async def list_pending(session_id: str | None = None) -> list[dict[str, Any]]:
    """取回待处理条目（可按会话过滤）；损坏条目跳过；Redis 故障返回空表。"""
    try:
        raw = await _redis.lrange(CACHE_KEY, 0, -1)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"显式缓存读取失败: {e}")
        return []
    entries: list[dict[str, Any]] = []
    for item in raw:
        try:
            data = json.loads(item)
        except Exception:  # noqa: BLE001 —— 损坏条目跳过
            continue
        if not isinstance(data, dict) or "message_id" not in data:
            continue
        if session_id is not None and data.get("session_id") != session_id:
            continue
        entries.append(data)
    return entries


async def clear_entries(message_ids: Iterable[int]) -> int:
    """清除指定 message_id 的条目（抽取成功后调用）；返回清除条数；Redis 故障不抛。

    实现：读全量 → 过滤 → 重写（单写者场景足够；LREM 需精确值匹配，重写更稳）。
    """
    target = {int(mid) for mid in message_ids}
    if not target:
        return 0
    try:
        raw = await _redis.lrange(CACHE_KEY, 0, -1)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"显式缓存清理失败: {e}")
        return 0
    keep: list[str] = []
    cleared = 0
    for item in raw:
        try:
            mid = int(json.loads(item).get("message_id"))
        except Exception:  # noqa: BLE001 —— 损坏条目丢弃（不算清除目标）
            continue
        if mid in target:
            cleared += 1
        else:
            keep.append(item)
    try:
        await _redis.delete(CACHE_KEY)
        if keep:
            await _redis.rpush(CACHE_KEY, *keep)
            await _redis.expire(CACHE_KEY, CACHE_TTL_SECONDS)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"显式缓存重写失败: {e}")
        return 0
    return cleared
