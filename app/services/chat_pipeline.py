"""聊天主管线（编排层）。

编排顺序（定稿）：
    会话锁 → 显式指令识别 → 记忆读取（缺页链）→ 组装包 → 生成 → 写回热层
    →（锁外）归档 → 写路径触发（显式暂存 + 水位攒批）→ 埋点
不做任何内容拦截（原安全过滤器已删除，见决策记录 D16）。

关键口径：
- 记忆读取前置且强制（黑盒接入点，系统设计 §4）：每轮先检索，再组装、再生成；
- 组装包五段由 short_term_memory.assemble_package 组装（纯函数），本模块负责准备
  素材：窗口段取热层窗口，判重窗口同样取"最近热层窗口"（不含本轮）；
- 写路径（E8 模块，零改动）：显式暂存 push_explicit 需归档后的 message_id；
  note_activity 三触发（水位攒批 / 重要声明 / 显式缓存）不阻塞主链路；
- 埋点：一轮一条 trace（chain / cost / health），fail-open 绝不打断对话；
  `cited` 留 None（引用检测后续补充）；`write_trigger` 为写路径触发原因。
- HTTP/SSE 适配留在 app/api/v1/endpoints/chat.py（薄适配层，含 meta 事件下发）。
"""
import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.core import short_term_memory as hot_layer
from app.core.database import AsyncSessionLocal
from app.core.logging import logger
from app.core.telemetry import emit_trace
from app.services import generation_service
from app.services.history_service import save_record_to_db
from app.services.long_term_memory import consolidator, explicit, injector, reranker, retriever

# 会话级进程内锁：同会话请求串行（替代原 Redis 分布式锁；单用户单进程场景足够）
_session_locks: dict[str, asyncio.Lock] = {}

# 前端 meta 事件的卡片摘要长度（"本轮记忆"可视化用）
_PREVIEW_CHARS = 30


def _lock_for(session_id: str) -> asyncio.Lock:
    if session_id not in _session_locks:
        _session_locks[session_id] = asyncio.Lock()
    return _session_locks[session_id]


# ============================================================
# 数据形态
# ============================================================

@dataclass
class MemoryContext:
    """记忆读取（黑盒口）的完整产出：注入文本 + 三段 trace（E12 埋点数据源）。"""

    text: str | None = None
    retrieval_trace: dict[str, Any] = field(default_factory=dict)   # retriever trace（含 stage_ms）
    rerank_trace: dict[str, Any] = field(default_factory=dict)      # reranker trace（E10）
    injector_trace: dict[str, Any] = field(default_factory=dict)    # render 输出：injected / skipped_window
    kept: list[Any] = field(default_factory=list)                   # ScoredCandidate 列表（meta 摘要用）


@dataclass
class TurnOutcome:
    """一轮对话的完整产出：回复文本（SSE 推流）+ 记忆元信息（meta 事件）。"""

    reply: str
    memory_meta: dict[str, Any] = field(default_factory=dict)


# ============================================================
# 记忆读取（黑盒接入点，E9/E10 已交付；E12 带 trace 回传）
# ============================================================

async def _retrieve_memory_context(session_id: str, user_message: str, *,
                                   window_texts: list[str]) -> MemoryContext:
    """记忆黑盒接入点（读）——retriever → reranker → injector 三段一体。

    完整链路：分诊 → 抽线索 → 扫快表 → 两级回源 → 顺线 → 精筛 → 注入格式。
    约定：过门槛返回注入文本（含类型/时间/状态标注）；未过门槛返回 None（"不召回"）。
    判重取"历史窗口"（window_texts 由调用方取热层窗口，本轮用户消息尚未入窗）。
    降级纪律：检索链路内建（gate / 语义路不抛）；DB 异常向上抛（底座问题必须暴露）。
    """
    async with AsyncSessionLocal() as session:
        retrieval = await retriever.retrieve(user_message, session=session)
        reranked = await reranker.rerank(user_message, retrieval.candidates)
    injector_trace: dict[str, Any] = {}
    text = injector.render(reranked.kept, window_texts=window_texts, trace=injector_trace)
    logger.debug(f"[pipeline] 记忆读取完成（会话 {session_id}）：候选 {len(retrieval.candidates)}，"
                 f"注入 {len(injector_trace.get('injected') or [])}")
    return MemoryContext(
        text=text,
        retrieval_trace=retrieval.trace,
        rerank_trace=reranked.trace,
        injector_trace=injector_trace,
        kept=list(reranked.kept),
    )


# ============================================================
# meta（"本轮记忆"可视化）
# ============================================================

def _build_memory_meta(mem: MemoryContext) -> dict[str, Any]:
    """组装 meta：none（未召回）/ injected（注入 N 条）+ 卡片摘要。"""
    injected_ids = mem.injector_trace.get("injected") or []
    if not injected_ids:
        return {"status": "none", "count": 0, "cards": []}
    by_id = {sc.candidate.ref_id: sc.candidate for sc in mem.kept}
    cards: list[dict[str, Any]] = []
    for ref_id in injected_ids:
        cand = by_id.get(ref_id)
        if cand is None or cand.kind == "raw":     # 原文候选（messages.id）不进面板摘要
            continue
        preview = cand.content.strip()
        if len(preview) > _PREVIEW_CHARS:
            preview = preview[:_PREVIEW_CHARS] + "…"
        cards.append({
            "id": cand.ref_id,
            "kind": cand.kind,
            "subtype": cand.subtype,
            "time": cand.event_time.isoformat() if cand.event_time else None,
            "preview": preview,
        })
    return {"status": "injected", "count": len(injected_ids), "cards": cards}


# ============================================================
# 埋点组装（E12 接线；字段契约见《配置与埋点文档》§2）
# ============================================================

def _compose_chain(mem: MemoryContext, write_trigger: str | None) -> dict[str, Any]:
    retrieval = mem.retrieval_trace or {}
    chain = {k: v for k, v in retrieval.items() if k != "stage_ms"}
    chain["rerank"] = mem.rerank_trace or None
    chain["injected"] = mem.injector_trace.get("injected") or []
    chain["skipped_window"] = mem.injector_trace.get("skipped_window") or []
    chain["cited"] = None       # 引用检测后续补充
    chain["write_trigger"] = write_trigger
    return chain


def _compose_cost(mem: MemoryContext, generate_ms: float,
                  gen: generation_service.GenerationResult, t0: float) -> dict[str, Any]:
    r_stage = (mem.retrieval_trace or {}).get("stage_ms") or {}
    rerank = mem.rerank_trace or {}
    rerank_stage = rerank.get("stage_ms") or {}
    gate = (mem.retrieval_trace or {}).get("gate") or {}
    return {
        "stage_ms": {
            "triage": r_stage.get("triage", 0.0),
            "retrieval": r_stage.get("retrieval", 0.0),
            "rerank": rerank_stage.get("rerank", 0.0),
            "generate": generate_ms,
            "total": round((time.perf_counter() - t0) * 1000, 1),
        },
        "llm_calls": {
            # gate 近似口径：模型/升级档采纳 ≈ 1 次判定调用；prefilter / disabled / 兜底 = 0
            "gate": 1 if gate.get("origin") in ("model", "upgrade") else 0,
            "rerank": rerank.get("judged", 0),
            "generate": 1,
        },
        "tokens": {
            "prompt": gen.prompt_tokens or 0,
            "completion": gen.completion_tokens or 0,
            "total": gen.total_tokens or 0,
        },
    }


def _compose_health(mem: MemoryContext, watermark_lag: int | None) -> dict[str, Any]:
    items = (mem.rerank_trace or {}).get("items") or []
    top = items[0] if items else {}
    return {
        "injected_count": len(mem.injector_trace.get("injected") or []),
        "top_score": top.get("score"),
        "confidence": top.get("confidence"),
        "watermark_lag": watermark_lag,
    }


async def _emit_turn_trace(*, trace_id: str, session_id: str, mem: MemoryContext,
                           generate_ms: float, gen: generation_service.GenerationResult,
                           write_trigger: str | None, t0: float) -> None:
    """一轮一条 trace（fail-open 双保险：水位报告失败记 None；组装/落盘失败只告警）。"""
    try:
        watermark_lag: int | None = None
        try:
            watermark_lag = (await consolidator.lag_report())["lag_messages"]
        except Exception as e:  # noqa: BLE001 —— 水位报告失败不拖累埋点
            logger.warning(f"[埋点] 水位报告获取失败（watermark_lag 记 None）: {e}")
        emit_trace(
            trace_id=trace_id,
            session_id=session_id,
            chain=_compose_chain(mem, write_trigger),
            cost=_compose_cost(mem, generate_ms, gen, t0),
            health=_compose_health(mem, watermark_lag),
        )
    except Exception as e:  # noqa: BLE001 —— 埋点任何异常不影响主链路
        logger.warning(f"[埋点] 本轮 trace 组装失败（已忽略）: {type(e).__name__}: {e}")


# ============================================================
# 主入口
# ============================================================

async def handle_message(session_id: str, user_message: str, *,
                         trace_id: str | None = None) -> TurnOutcome:
    """处理一条用户消息，返回一轮完整产出（回复 + 记忆 meta）。

    锁内（读取 → 组装 → 生成 → 写回热层）保证同会话串行与窗口一致性；
    锁外（归档 → 写路径触发 → 埋点）全程降级静默——对话不能挂一次。
    """
    trace_id = trace_id or uuid.uuid4().hex
    t0 = time.perf_counter()

    async with _lock_for(session_id):
        # ① 显式指令识别（零成本正则；暂存随归档——push_explicit 需要 message_id）
        explicit_hit = explicit.is_explicit(user_message)

        # ② 素材：热层窗口（既是组装包的窗口段，也是记忆判重的比对基准）
        hot_window = await hot_layer.get_history(session_id)

        # ③ 记忆读取（黑盒，服务端强制前置）
        mem = await _retrieve_memory_context(session_id, user_message,
                                             window_texts=[m.content for m in hot_window])

        # ④ 组装包五段 → ⑤ 统一生成（全系统唯一 LLM 入口）
        package = hot_layer.assemble_package(
            current_input=user_message,
            window=hot_window,
            memory_context=mem.text,
        )
        gen_t0 = time.perf_counter()
        gen = await generation_service.generate_reply(package)
        generate_ms = round((time.perf_counter() - gen_t0) * 1000, 1)

        # ⑥ 写回热层（本轮两条——下一轮窗口素材；B2 基线通路永远存在）
        await hot_layer.add_message(session_id, "user", user_message)
        await hot_layer.add_message(session_id, "assistant", gen.reply)

    # ⑦ 归档明文（失败仅记日志；id 供显式暂存）
    user_msg_id = await save_record_to_db(session_id, "user", user_message)
    await save_record_to_db(session_id, "assistant", gen.reply)

    # ⑧ 写路径触发（E8 模块零改动；失败静默——push_explicit 显式零延迟 + note_activity 三触发）
    if explicit_hit and user_msg_id is not None:
        await explicit.push_explicit(session_id=session_id, message_id=user_msg_id,
                                     content=user_message)
    write_trigger = await consolidator.note_activity(session_id=session_id)

    # ⑨ 埋点（一轮一条 trace；fail-open）
    await _emit_turn_trace(trace_id=trace_id, session_id=session_id, mem=mem,
                           generate_ms=generate_ms, gen=gen,
                           write_trigger=write_trigger, t0=t0)

    logger.info(f"[Trace: {trace_id}] 会话 {session_id} 完成一轮对话"
                f"（回复 {len(gen.reply)} 字符；读 {len(mem.injector_trace.get('injected') or [])} 张；"
                f"写触发 {write_trigger or '无'}）")
    return TurnOutcome(reply=gen.reply, memory_meta=_build_memory_meta(mem))
