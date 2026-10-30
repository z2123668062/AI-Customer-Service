"""主链路联调测试：组装包 / 顺序断言 / 埋点字段 / meta / SSE。

全部依赖 mock（不碰真实 LLM / Redis / 数据库文件）；按链路断言关键分派行为。
"""
import json
from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.core import short_term_memory as hot_layer
from app.main import app
from app.models.schemas import ChatMessage
from app.services import chat_pipeline, generation_service
from app.services.chat_pipeline import MemoryContext
from app.services.generation_service import GenerationResult

client = TestClient(app)


# ================================
# 组装包（短记忆 + 五段结构）
# ================================

def test_assemble_package_five_segments():
    """五段结构：系统提示 / 注入 / 任务卡占位（恒空）/ 窗口 / 当前输入。"""
    win = [ChatMessage(role="user", content="历史消息")]
    pkg = hot_layer.assemble_package(current_input="现在的问题", window=win,
                                     memory_context="注入素材")
    assert pkg.system_prompt == hot_layer.SYSTEM_PROMPT
    assert pkg.memory_injection == "注入素材"
    assert pkg.task_card is None                 # V1 恒空（显式占位）
    assert pkg.window == win
    assert pkg.current_input == "现在的问题"


def test_assemble_package_without_memory_has_no_injection():
    """没有召回 → 不装配注入段（生成行为回落到纯窗口）。"""
    pkg = hot_layer.assemble_package(current_input="x")
    assert pkg.memory_injection is None


def test_render_messages_five_segments():
    """渲染顺序：①system → ②注入(附加 system) → ③任务卡(恒空跳过) → ④窗口(空白跳过) → ⑤当前输入。"""
    pkg = hot_layer.assemble_package(
        current_input="当前问题",
        window=[
            ChatMessage(role="user", content="历史一"),
            ChatMessage(role="assistant", content="历史二"),
            ChatMessage(role="user", content="   "),   # 空白消息不产消息（兼容端纪律）
        ],
        memory_context="注入段",
    )
    msgs = generation_service.render_messages(pkg)
    assert [m["role"] for m in msgs] == ["system", "system", "user", "assistant", "user"]
    assert msgs[0]["content"] == hot_layer.SYSTEM_PROMPT
    assert msgs[1]["content"] == "注入段"
    assert msgs[-1]["content"] == "当前问题"


# ================================
# 主链路：统一依赖假件 + 顺序断言
# ================================

def _install_pipeline_fakes(monkeypatch, *, hot_window=(), retrieve_text=None, save_return=42):
    """安装主链路全依赖假件（monkeypatch 自动恢复）；返回记录器。

    rec["events"] 记录关键调用顺序；rec["packages"] 收集交给生成的组装包。
    """
    rec = {"events": [], "packages": [], "counts": defaultdict(int), "captured": {}}
    ev = rec["events"]

    class _SessionCtx:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            return False

    def fake_is_explicit(text):
        ev.append("explicit.is_explicit")
        rec["counts"][f"explicit:{text}"] += 1
        return text.startswith("记住")

    async def fake_get_history(sid):
        ev.append("hot.get_history")
        rec["counts"]["hot"] += 1
        return list(hot_window)

    async def fake_retrieve(query, *, session=None, now=None):
        ev.append("retriever.retrieve")
        rec["counts"]["retrieve"] += 1
        from app.services.long_term_memory import retriever as r
        return r.RetrievalResult(
            candidates=["C1"],
            trace={"triage": "fault", "stage_ms": {"triage": 1.0, "retrieval": 2.0}},
        )

    async def fake_rerank(query, candidates):
        ev.append("reranker.rerank")
        from app.services.long_term_memory import reranker as rr
        return rr.RerankResult(kept=[], trace={"judged": 0, "items": [],
                                               "stage_ms": {"rerank": 3.0}})

    def fake_render(memories, *, window_texts=(), trace=None):
        ev.append("injector.render")
        if trace is not None and retrieve_text:
            trace.update({"injected": ["C1"], "skipped_window": []})
        return retrieve_text

    async def fake_generate(package):
        ev.append("generate_reply")
        rec["packages"].append(package)
        return GenerationResult(reply="假回复")

    async def fake_add(sid, role, content):
        ev.append(f"hot.add_message:{role}")

    async def fake_save(sid, role, content):
        ev.append(f"save_record:{role}")
        return save_return

    async def fake_push(**kwargs):
        ev.append("explicit.push_explicit")
        rec["captured"]["push"] = kwargs
        return True

    async def fake_note(**kwargs):
        ev.append("consolidator.note_activity")
        rec["counts"]["note"] += 1
        return "rounds"

    async def fake_lag():
        ev.append("consolidator.lag_report")
        return {"lag_messages": 4}

    def fake_emit(**kwargs):
        ev.append("emit_trace")
        rec["captured"]["trace"] = kwargs
        return True

    monkeypatch.setattr(chat_pipeline.explicit, "is_explicit", fake_is_explicit)
    monkeypatch.setattr(chat_pipeline.hot_layer, "get_history", fake_get_history)
    monkeypatch.setattr(chat_pipeline.retriever, "retrieve", fake_retrieve)
    monkeypatch.setattr(chat_pipeline.reranker, "rerank", fake_rerank)
    monkeypatch.setattr(chat_pipeline.injector, "render", fake_render)
    monkeypatch.setattr(chat_pipeline.generation_service, "generate_reply", fake_generate)
    monkeypatch.setattr(chat_pipeline.hot_layer, "add_message", fake_add)
    monkeypatch.setattr(chat_pipeline, "save_record_to_db", fake_save)
    monkeypatch.setattr(chat_pipeline.explicit, "push_explicit", fake_push)
    monkeypatch.setattr(chat_pipeline.consolidator, "note_activity", fake_note)
    monkeypatch.setattr(chat_pipeline.consolidator, "lag_report", fake_lag)
    monkeypatch.setattr(chat_pipeline, "emit_trace", fake_emit)
    monkeypatch.setattr(chat_pipeline, "AsyncSessionLocal", lambda: _SessionCtx())
    return rec


@pytest.mark.asyncio
async def test_handle_message_main_chain_order(monkeypatch):
    """主链路顺序断言：显式识别 → 读取 → 组装 → 生成 → 热层回写 → 归档 → 写触发 → 埋点。"""
    rec = _install_pipeline_fakes(monkeypatch, retrieve_text="注入素材")

    outcome = await chat_pipeline.handle_message("s1", "你好呀", trace_id="t-order")

    assert rec["events"] == [
        "explicit.is_explicit",
        "hot.get_history",
        "retriever.retrieve",
        "reranker.rerank",
        "injector.render",
        "generate_reply",
        "hot.add_message:user",
        "hot.add_message:assistant",
        "save_record:user",
        "save_record:assistant",
        "consolidator.note_activity",
        "consolidator.lag_report",
        "emit_trace",
    ]
    assert outcome.reply == "假回复"
    package = rec["packages"][0]
    assert package.current_input == "你好呀"
    assert package.memory_injection == "注入素材"


@pytest.mark.asyncio
async def test_handle_message_explicit_push_after_archive(monkeypatch):
    """显式通道接线：识别在读取前；暂存在归档后（message_id 来自归档）。"""
    rec = _install_pipeline_fakes(monkeypatch, save_return=42)

    await chat_pipeline.handle_message("s1", "记住我的车钥匙放玄关了")

    idx_identify = rec["events"].index("explicit.is_explicit")
    idx_retrieve = rec["events"].index("hot.get_history")
    idx_save = rec["events"].index("save_record:user")
    idx_push = rec["events"].index("explicit.push_explicit")
    assert idx_identify < idx_retrieve < idx_save < idx_push    # 顺序纪律
    assert rec["captured"]["push"] == {
        "session_id": "s1", "message_id": 42, "content": "记住我的车钥匙放玄关了"}


@pytest.mark.asyncio
async def test_handle_message_no_push_when_not_explicit(monkeypatch):
    """非显式消息：不触发暂存（is_explicit 未命中）。"""
    rec = _install_pipeline_fakes(monkeypatch)
    await chat_pipeline.handle_message("s1", "普通消息")
    assert "explicit.push_explicit" not in rec["events"]


# ================================
# 埋点字段组装（chain / cost / health）
# ================================

@pytest.mark.asyncio
async def test_emit_turn_trace_fields(monkeypatch):
    rec = {"captured": {}}

    async def fake_lag():
        return {"lag_messages": 5}

    def fake_emit(**kwargs):
        rec["captured"].update(kwargs)
        return True

    monkeypatch.setattr(chat_pipeline.consolidator, "lag_report", fake_lag)
    monkeypatch.setattr(chat_pipeline, "emit_trace", fake_emit)

    mem = MemoryContext(
        text="注入",
        retrieval_trace={"triage": "fault", "entities": ["花生"], "gate": {"origin": "model"},
                         "stage_ms": {"triage": 12.5, "retrieval": 30.2}},
        rerank_trace={"judged": 2, "items": [{"score": 0.9, "confidence": 0.8}],
                      "stage_ms": {"rerank": 55.0}},
        injector_trace={"injected": [7], "skipped_window": [9]},
        kept=[],
    )
    gen = GenerationResult(reply="hi", prompt_tokens=100, completion_tokens=20, total_tokens=120)

    import time as _time
    await chat_pipeline._emit_turn_trace(trace_id="t1", session_id="s1", mem=mem,
                                         generate_ms=88.0, gen=gen,
                                         write_trigger="rounds",
                                         t0=_time.perf_counter() - 0.01)
    out = rec["captured"]
    assert out["trace_id"] == "t1" and out["session_id"] == "s1"

    chain = out["chain"]
    assert chain["triage"] == "fault" and chain["entities"] == ["花生"]
    assert chain["rerank"]["judged"] == 2
    assert chain["injected"] == [7] and chain["skipped_window"] == [9]
    assert chain["cited"] is None and chain["write_trigger"] == "rounds"
    assert "stage_ms" not in chain                # 耗时归 cost 组（不重复入 chain）

    cost = out["cost"]
    assert cost["stage_ms"]["triage"] == 12.5 and cost["stage_ms"]["retrieval"] == 30.2
    assert cost["stage_ms"]["rerank"] == 55.0 and cost["stage_ms"]["generate"] == 88.0
    assert cost["stage_ms"]["total"] > 0
    assert cost["llm_calls"] == {"gate": 1, "rerank": 2, "generate": 1}
    assert cost["tokens"] == {"prompt": 100, "completion": 20, "total": 120}

    health = out["health"]
    assert health["injected_count"] == 1 and health["top_score"] == 0.9
    assert health["confidence"] == 0.8 and health["watermark_lag"] == 5


# ================================
# meta 与 SSE 下发
# ================================

def test_build_memory_meta_two_states():
    """meta 两态：none（未召回）/ injected（注入 N 条，附卡片摘要）。"""
    assert chat_pipeline._build_memory_meta(MemoryContext())["status"] == "none"

    cand = SimpleNamespace(ref_id=7, kind="entity", subtype="preference",
                           content="x" * 50, event_time=None)
    mem = MemoryContext(injector_trace={"injected": [7], "skipped_window": []},
                        kept=[SimpleNamespace(candidate=cand)])
    meta = chat_pipeline._build_memory_meta(mem)
    assert meta["status"] == "injected" and meta["count"] == 1
    assert meta["cards"][0]["id"] == 7 and meta["cards"][0]["subtype"] == "preference"
    assert meta["cards"][0]["preview"] == "x" * 30 + "…"        # 摘要截断

    raw = SimpleNamespace(ref_id=99, kind="raw", subtype=None, content="原文", event_time=None)
    mem2 = MemoryContext(injector_trace={"injected": [99]}, kept=[SimpleNamespace(candidate=raw)])
    meta2 = chat_pipeline._build_memory_meta(mem2)
    assert meta2["status"] == "injected" and meta2["count"] == 1 and meta2["cards"] == []


def test_chat_sse_meta_event_injected(mock_redis, monkeypatch):
    """端到端：注入场景的 meta 事件（content 为对象）在 message 之前下发。"""
    cand = SimpleNamespace(ref_id=7, kind="entity", subtype="preference",
                           content="喜欢喝美式咖啡", event_time=None)
    injected = MemoryContext(text="注入", injector_trace={"injected": [7], "skipped_window": []},
                             kept=[SimpleNamespace(candidate=cand)])
    with patch("app.core.short_term_memory.redis_client", mock_redis), \
            patch("app.services.generation_service.generate_reply", new_callable=AsyncMock,
                  return_value=GenerationResult(reply="好的")) as mock_generate, \
            patch("app.services.chat_pipeline.save_record_to_db", new_callable=AsyncMock,
                  return_value=1), \
            patch("app.services.chat_pipeline._retrieve_memory_context",
                  new_callable=AsyncMock, return_value=injected), \
            patch("app.services.chat_pipeline._emit_turn_trace", new_callable=AsyncMock), \
            patch("app.services.chat_pipeline.consolidator.note_activity",
                  new_callable=AsyncMock, return_value=None):
        response = client.post("/api/v1/chat/", json={"session_id": "test_meta_001",
                                                      "message": "我喝什么咖啡来着"})

    assert response.status_code == 200
    events = [json.loads(line[6:]) for line in response.content.decode("utf-8").split("\n")
              if line.startswith("data: ")]
    metas = [e for e in events if e["event"] == "meta"]
    assert len(metas) == 1
    meta = metas[0]["content"]                       # content 为对象（非字符串）
    assert meta["status"] == "injected" and meta["count"] == 1
    assert meta["cards"][0]["preview"] == "喜欢喝美式咖啡"
    # 顺序：status → meta → message…
    kinds = [e["event"] for e in events]
    assert kinds.index("status") < kinds.index("meta") < kinds.index("message")


# ================================
# 组装包渲染（generation_service.render_messages）五段全量
# ================================

def test_generation_result_carries_usage_tokens():
    """GenerationResult 携带 token 用量（埋点 cost.tokens 数据源）。"""
    gen = GenerationResult(reply="x", prompt_tokens=1, completion_tokens=2, total_tokens=3)
    assert (gen.prompt_tokens, gen.completion_tokens, gen.total_tokens) == (1, 2, 3)
