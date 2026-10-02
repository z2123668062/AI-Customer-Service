# app/services/long_term_memory/consolidator.py
"""写路径 · 写入编排（E8；D23 全局水位重构）：触发判断 → 后台串行抽取 → journal 推进 → 启动补偿。

三条触发条件（note_activity，消息归档后调用；E12 在主链路归档后接线）：
① 攒批：全局待抽消息 ≥ EXTRACT_WATERMARK_ROUNDS × 2（一轮≈两条：用户+助手；D23 全局口径）；
② 重要声明：新消息命中“我叫/我住在/我过敏…”等模式（提前暖场；宁宽勿窄——抽取器会过滤）；
③ 空闲收口：全局静默 ≥ EXTRACT_IDLE_TIMEOUT_MINUTES 且仍落后（后台巡检周期触发）。
优先级：rounds > explicit（批覆盖清账兜底）；②与显式指令重叠时让位单抽（保 source="explicit"
标签、批延后到下一触发）——E12 联调定案。

两条通道：
- 被动批（_consolidate）：按全局水位取批 → extractor.extract_batch → writer 三固定动作 → 落库
  → journal 追记成功行（水位推进的唯一时点）；
- 显式单（_process_explicit）：Redis 待办条目 → extractor.extract_explicit（不等攒批）→ 落库打
  source="explicit"；成功后清缓存并使该消息退出批抽取（_explicit_handled 防重复建卡）。

一致性设计（D23——全局水位 + journal 日志载体；替代 D22 的表方案）：
- **水位真相 = journal 文件**（追加式日志 `data/extract_journal.log`）：最后一条 `watermark=N`
  即进度；尾行损坏 → 回退上一条（重抽，方向安全）；文件缺失 → 首启画线（跳过存量）；
- **撕裂窗口**（卡片已提交、journal 未写入 → 该批重抽）按 D23 接受：由“完全相同卡判重
  （E12 接活）+ 取代判定”兜底——宁可重抽、不可丢；
- 全局单飞（单一 asyncio.Lock）：批与显式互不重叠（单进程单写者 = D23 前提）；
- 后台任务不阻塞主链路（asyncio.create_task）；失败写 journal 失败行（水位不动）；
- WRITE_MODE=off（B1/B2 档）→ 所有触发点直接返回；raw_chunks（B4′）→ 跳过 LLM 抽取与判定。

启动与巡检（main.py lifespan 调用；接线说明书见 docs/写路径文档.md）：
- startup_scan：journal 无记录 → 首启画线（跳过存量）；水位超前（换库/删库）→ 告警 + 对齐；
  水位落后 → 补抽；已追平 → 无事；
- reap_idle_sessions：全局空闲收口（静默超时且仍落后 → 收口）；
- backfill：手动补抽（运维/测试/换库迁移后；from_zero 可整段重抽）。

测试钩子：SessionFactory / journal._journal_file 可 monkeypatch（注入独立库与临时日志）；
wait_idle() 等后台任务；reset_for_tests() 清模块状态（全局锁/已处理集合/任务表/journal 缓存）。
"""
from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.core.logging import logger
from app.services.long_term_memory import explicit, extractor, journal, store, writer
from app.services.long_term_memory.extractor import CardDraft

# —— 内部常量（非实验旋钮）——
_BATCH_LIMIT = 200                 # 单轮最多处理的消息数（启动补偿/巡检分批推进）
_NOTE_SCAN_LIMIT = 30              # note_activity 检查重要声明时最多回看的消息数
_REAPER_INTERVAL_SECONDS = 300.0   # 空闲巡检周期（5 分钟）
_HANDLED_SOFT_LIMIT = 10000        # _explicit_handled 软上限（超过即清空——重启级重置语义）

# 重要声明模式（提前触发的暖场；宁宽勿窄——多抽一次无代价）
_IMPORTANT_RE = re.compile(
    r"我叫|我的名字|我住在|我家在|我搬到|我的生日|我出生|我过敏|对.{1,10}过敏|"
    r"我喜欢|我不喜欢|我讨厌|我习惯|我计划|我打算|我准备|我决定|"
    r"我老婆|我老公|我妻子|我丈夫|我儿子|我女儿|我爸|我妈|我哥|我姐|我弟|我妹"
)

# —— 模块状态（测试经 monkeypatch / reset_for_tests 清理）——
SessionFactory: async_sessionmaker = AsyncSessionLocal    # 会话工厂（测试注入独立库）
_run_lock: asyncio.Lock | None = None  # 全局单飞锁（批与显式互不重叠；D23 单写者前提）
_explicit_handled: set[int] = set()    # 已被显式单抽处理的消息 id（批抽取跳过——防重复建卡）
_tasks: set[asyncio.Task] = set()      # 在飞后台任务
_reaper_task: asyncio.Task | None = None


# ============ 小工具 ============

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(dt: datetime | None) -> datetime | None:
    """统一时区（SQLite 读回的 datetime 可能无 tzinfo——按 UTC 补全，防比较 TypeError）。"""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _lock() -> asyncio.Lock:
    """全局单飞锁（懒创建——避免模块导入期绑定事件循环；D23 全局口径）。"""
    global _run_lock
    if _run_lock is None:
        _run_lock = asyncio.Lock()
    return _run_lock


def _spawn(coro) -> asyncio.Task:
    """拉起后台任务（不阻塞调用方；异常由任务主体自兜底）。"""
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


# ============ 触发与编排 ============

async def note_activity(*, session_id: str) -> str | None:
    """消息归档后的写路径钩子（E12 在主链路归档提交后调用；E8 由测试/运维直接调用）。

    全局触发判断（D23——水位跨会话共享）：待抽 = 全局最新消息 id − journal 水位；
    命中则拉起后台任务（不阻塞调用方）。返回触发原因（"rounds" / "important" / "explicit" / None）。

    优先级：rounds 场景批照拉（explicit 并行、批先取锁——批覆盖清账兜底）；
    important 与 explicit 重叠（"记住：我住在…"同中两模式）时让位单抽、批延后
    （保 source="explicit" 标签；该消息单抽后即退出批——无信息丢失）。

    session_id 仅作日志溯源（触发与水位已全局化；E12 接线签名不变）。
    """
    if not settings.memory_write_enabled:
        return None
    reason: str | None = None
    try:
        async with SessionFactory() as session:
            max_id, _ = await store.get_global_message_stats(session)
            watermark = journal.current()
            pending = max(0, max_id - watermark)
            if pending >= settings.EXTRACT_WATERMARK_ROUNDS * 2:
                reason = "rounds"
            elif pending > 0:
                fresh = await store.list_messages(session, after_id=watermark,
                                                  limit=_NOTE_SCAN_LIMIT)
                if any(m.role == "user" and _IMPORTANT_RE.search(m.content or "") for m in fresh):
                    reason = "important"
    except Exception as e:  # noqa: BLE001 —— 钩子失败不影响主链路
        logger.warning(f"[consolidator] note_activity 失败（会话 {session_id}；跳过本轮触发）: "
                       f"{type(e).__name__}: {e}")
        return None
    # 显式缓存检查先于批拉起判断（Redis 故障静默降级为“批抽取兜底”；D23 全局扫）
    entries = await explicit.list_pending()
    if reason == "important" and entries:
        # 重叠让位：保显式标签，批延后（rounds / idle / 下轮消息随时触发兜底）
        logger.info("[consolidator] important 与 explicit 重叠：让位单抽（保标签），批延后")
        reason = "explicit"
    elif reason:
        _spawn(_consolidate(reason=reason))
    if entries:
        _spawn(_process_explicit())
        reason = reason or "explicit"
    return reason


async def _consolidate(*, reason: str) -> writer.IngestResult | None:
    """后台批抽取任务主体（被动通道；全局单飞）。失败写 journal 失败行、水位不动（自动补抽）。"""
    if not settings.memory_write_enabled:
        return None
    async with _lock():
        try:
            result = await _run_batch(reason=reason)
            if result is not None:
                logger.info(f"[consolidator] 批抽取完成（{reason}）：建卡 {result.cards_created}，"
                            f"取代 {result.superseded}，连边 {result.edges_created}")
            return result
        except Exception as e:  # noqa: BLE001 —— 任何失败：journal 失败行 + 水位不动
            logger.exception(f"[consolidator] 批抽取失败（{reason}）")
            await _record_failure(e)
            return None


async def _run_batch(*, reason: str) -> writer.IngestResult | None:
    """单轮批抽取：读批 → 抽取 → 判定/落库 → journal 追记成功行（水位推进的唯一时点）。"""
    started = time.monotonic()
    # ① 读批（短事务，随即释放；全局水位为起点——D23）
    watermark = journal.current()
    async with SessionFactory() as session:
        messages = await store.list_messages(session, after_id=watermark, limit=_BATCH_LIMIT)
        await session.commit()
    if not messages:
        return None                                     # 无新消息：空转退出（锁串行下安全）
    batch = [m for m in messages if m.id not in _explicit_handled]
    end_id = messages[-1].id
    sessions = {m.session_id for m in messages if m.session_id}
    origin = next(iter(sessions)) if len(sessions) == 1 else None   # 单会话保真溯源；混批留空
    # ② 抽取（事务外）
    if settings.WRITE_MODE == "raw_chunks":             # B4′：原文切块直建（零抽取零判定）
        drafts: list[CardDraft] = extractor.dedupe_drafts(extractor.build_raw_chunks(batch))
        with_plan = False
    else:
        extracted = await extractor.extract_batch(batch)
        if extracted is None:
            raise RuntimeError("抽取失败：全部消息段均未产出（水位保持不动，下轮重试）")
        drafts = extractor.dedupe_drafts(extracted)
        with_plan = True
    # ③ 落库 + 同步（一个事务收口）
    result = await _ingest(session_id=origin, drafts=drafts, source="passive", with_plan=with_plan)
    # ④ journal 追记成功行（★ 水位推进的唯一时点：DB 提交成功之后追加）
    line = journal.format_success(
        watermark=end_id, messages=len(batch), drafts=drafts,
        duplicates=result.duplicates,
        superseded=result.superseded, edges=result.edges_created,
        sessions=sessions, elapsed=time.monotonic() - started)
    if not journal.advance(end_id, line):
        logger.warning("[consolidator] journal 写入失败：水位未推进（本批下轮将重抽——D23 撕裂窗口）")
    # ⑤ 批覆盖范围内的显式条目清账（缓存里还在的 = 单抽未处理过的；批已兜底抽取）
    try:
        covered = [int(e["message_id"]) for e in await explicit.list_pending()
                   if "message_id" in e and watermark < int(e["message_id"]) <= end_id]
        if covered:
            _explicit_handled.update(covered)
            await explicit.clear_entries(covered)
    except Exception as e:  # noqa: BLE001 —— 清账失败不影响主流程（下轮重扫无害）
        logger.warning(f"[consolidator] 显式清账失败（不影响批抽取）: {e}")
    return result


async def _process_explicit() -> int:
    """后台显式单抽任务（主动通道；全局单飞）；返回处理条目数。

    扫全量待办并按会话分组处理（source_session_id 溯源保真）；
    成功（含“抽完没信息”）→ 清缓存 + 记入 _explicit_handled；
    失败 → 保留缓存（下轮重试；批抽取亦会兜底）。
    """
    if not settings.memory_write_enabled:
        return 0
    async with _lock():
        try:
            entries = [e for e in await explicit.list_pending()
                       if int(e.get("message_id", -1)) not in _explicit_handled]
            if not entries:
                return 0
            groups: dict[str, list[dict]] = {}
            for e in entries:
                groups.setdefault(str(e.get("session_id") or ""), []).append(e)
            handled = 0
            for sid, group in groups.items():
                drafts = await extractor.extract_explicit(group)
                if drafts is None:
                    logger.warning(f"[consolidator] 会话 {sid} 显式抽取失败（保留缓存重试）")
                    continue
                drafts = extractor.dedupe_drafts(drafts)
                created = 0
                if drafts:
                    result = await _ingest(session_id=sid or None, drafts=drafts, source="explicit")
                    created = result.cards_created
                ids = [int(e["message_id"]) for e in group if "message_id" in e]
                _explicit_handled.update(ids)
                handled += len(ids)
                await explicit.clear_entries(ids)
                logger.info(f"[consolidator] 会话 {sid} 显式抽取完成：条目 {len(ids)}，建卡 {created}")
            if len(_explicit_handled) > _HANDLED_SOFT_LIMIT:
                _explicit_handled.clear()               # 软上限：重启级重置（水位已保证不重抽）
            return handled
        except Exception as e:  # noqa: BLE001
            logger.exception("[consolidator] 显式抽取异常")
            await _record_failure(e)
            return 0


async def _ingest(*, session_id: str | None, drafts: list[CardDraft], source: str,
                  with_plan: bool = True) -> writer.IngestResult:
    """共享半程：候选快照（短事务）→ 事务外判定 → 落库 + 同步（一个事务收口）。

    水位推进**不在这里**（D23：journal 独立于 DB 事务，由批路径 _run_batch 在提交后追记）。
    session_id = 卡片溯源会话（全局混批时为 None——“不知道来自哪个会话”好于标错）。
    """
    if with_plan and drafts:
        async with SessionFactory() as session:
            candidates = await writer.load_candidates(session, drafts=drafts)
            await session.commit()
        plan = await writer.plan_batch(drafts=drafts, candidates=candidates)
    else:
        plan = writer.BatchPlan(drafts=list(drafts))
    async with SessionFactory() as session:
        result = await writer.apply_batch(session, plan=plan, session_id=session_id, source=source)
        await store.commit_and_sync(session, upsert_cards=result.cards,
                                    upsert_edges=result.edges)
    return result


async def _record_failure(exc: Exception) -> None:
    """失败记录：写 journal 失败行（水位保持不动；尽力而为——记录失败不抛）。"""
    line = journal.format_failure(watermark=journal.current(),
                                  error=f"{type(exc).__name__}: {exc}")
    journal.append_record(line)                         # 内部含告警；失败仅失去留痕


# ============ 启动补偿 / 空闲收口 / 手动补抽 ============

async def startup_scan() -> dict[str, Any]:
    """启动补偿扫描（lifespan 调用；不阻塞——命中则拉后台任务）。

    全局三态（D23）：
    - journal 无有效记录 + 有存量消息 → **首启画线**（水位=最新消息 id，跳过存量，
      避免启动即全量重抽；需要补抽用 backfill）；
    - 水位 > 最新消息 id（换库/删库残留）→ 告警 + **自检对齐**（降为最新 id）；
    - 水位 < 最新消息 id → 拉后台补抽；水位 == 最新 id → 无事。

    返回 {action: off/empty/first_run_line/self_heal/backfill/aligned/error, ...}。
    """
    if not settings.memory_write_enabled:
        return {"action": "off"}
    try:
        async with SessionFactory() as session:
            max_id, _ = await store.get_global_message_stats(session)
    except Exception as e:  # noqa: BLE001 —— 启动永不阻塞
        logger.warning(f"[consolidator] 启动扫描失败: {e}")
        return {"action": "error", "error": str(e)}
    w = journal.read_watermark()                        # 启动以文件为真相（顺带刷新认知）
    if w is None:                                        # 首启（或无有效记录）
        if max_id > 0:                                   # 有存量：画线跳过（防启动即全量重抽）
            line = journal.format_record(
                event="首启画线", watermark=max_id,
                detail=f"跳过存量 {max_id} 条（避免启动即全量重抽；需要补抽用 backfill）")
            journal.append_record(line)
            journal.prime(max_id)
            logger.info(f"[consolidator] 首启画线：存量 {max_id} 条跳过（backfill 可补）")
            return {"action": "first_run_line", "watermark": max_id, "latest_message_id": max_id}
        journal.prime(0)
        return {"action": "empty", "watermark": 0, "latest_message_id": 0}
    if w > max_id:                                       # 自检：水位超前（换库/删库）
        line = journal.format_record(
            event="自检对齐", watermark=max_id,
            detail=f"原水位 {w} 超前于最新消息 id {max_id}（换库/删库），已对齐")
        journal.append_record(line)
        journal.prime(max_id)
        logger.warning(f"[consolidator] 自检：水位 {w} > 消息 id {max_id}，已对齐 {max_id}")
        return {"action": "self_heal", "watermark": max_id, "latest_message_id": max_id}
    journal.prime(w)
    if w < max_id:
        _spawn(_consolidate(reason="startup_backfill"))
        logger.info(f"[consolidator] 启动补偿：水位 {w} 落后 {max_id}，已拉起补抽")
        return {"action": "backfill", "watermark": w, "latest_message_id": max_id}
    return {"action": "aligned", "watermark": w, "latest_message_id": max_id}


async def reap_idle_sessions(*, idle_minutes: int | None = None) -> bool:
    """空闲收口巡检（全局版，D23）：全局静默超时且仍落后 → 收口抽取。返回是否触发。

    静默计时 = 全局最新消息时间（messages.created_at 聚合；不再需要活动时间列）。
    消费方：后台巡检循环（start_idle_reaper）与测试（直接调用，不启动循环）。
    """
    if not settings.memory_write_enabled:
        return False
    idle = idle_minutes if idle_minutes is not None else settings.EXTRACT_IDLE_TIMEOUT_MINUTES
    threshold = _now() - timedelta(minutes=idle)
    try:
        async with SessionFactory() as session:
            max_id, latest = await store.get_global_message_stats(session)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[consolidator] 空闲巡检读取失败: {e}")
        return False
    if max_id <= journal.current():                     # 已追平：无待收口
        return False
    latest_utc = _as_utc(latest)
    if latest_utc is None or latest_utc > threshold:    # 仍在活动
        return False
    logger.info(f"[consolidator] 空闲收口：静默超时且落后（水位 {journal.current()} < {max_id}）")
    _spawn(_consolidate(reason="idle"))
    return True


async def backfill(*, drain: bool = True, max_rounds: int = 20, from_zero: bool = False) -> int:
    """手动补抽（运维/测试/换库迁移后）——**同步执行**（调用方等待完成）。

    drain=True 时循环推进直到无新消息或失败（受 max_rounds 上限保护）；返回处理的轮数。
    from_zero=True：先重置内存水位为 0（从最早一条开始补——换库迁移后整段重抽；
    重置不写文件，重抽后的成功行接管水位认知——半途失败时文件水位仍是旧值，方向安全）。
    """
    if not settings.memory_write_enabled:
        return 0
    if from_zero:
        journal.prime(0)
    rounds = 0
    while rounds < (max_rounds if drain else 1):
        result = await _consolidate(reason="backfill")
        rounds += 1
        if result is None:                              # 无新消息或失败：停止
            break
        async with SessionFactory() as session:
            max_id, _ = await store.get_global_message_stats(session)
        if max_id <= journal.current():
            break
    return rounds


async def lag_report() -> dict[str, Any]:
    """写路径滞后报告（全局单点，D23）：水位 / 落后条数 / 最新消息时间 / 最后一条 journal 记录。"""
    async with SessionFactory() as session:
        max_id, latest = await store.get_global_message_stats(session)
    watermark = journal.current()
    return {
        "watermark": watermark,
        "latest_message_id": max_id,
        "lag_messages": max(0, max_id - watermark),
        "latest_message_at": _as_utc(latest),
        "last_record": journal.read_last_record(),
    }


# ============ 空闲巡检循环 / 测试钩子 ============

def start_idle_reaper(*, interval_seconds: float | None = None) -> None:
    """启动空闲收口巡检循环（lifespan 调用；幂等）。"""
    global _reaper_task
    if _reaper_task is None or _reaper_task.done():
        _reaper_task = asyncio.create_task(
            _reaper_loop(interval_seconds if interval_seconds is not None else _REAPER_INTERVAL_SECONDS))


async def _reaper_loop(interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            await reap_idle_sessions()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 —— 巡检异常不致命
            logger.warning(f"[consolidator] 空闲巡检异常: {e}")


async def stop_idle_reaper() -> None:
    """停止巡检循环（lifespan 关闭调用；幂等）。"""
    global _reaper_task
    task, _reaper_task = _reaper_task, None
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def wait_idle(timeout: float = 30.0) -> None:
    """等待全部在飞后台任务结束（测试辅助；生产 shutdown 亦可用）。"""
    deadline = time.monotonic() + timeout
    while _tasks and time.monotonic() < deadline:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


def reset_for_tests() -> None:
    """清空模块状态（测试 fixture 调用；生产不使用；应在 wait_idle 之后调用）。"""
    global _run_lock
    _run_lock = None
    _explicit_handled.clear()
    _tasks.clear()
    journal.reset_for_tests()
