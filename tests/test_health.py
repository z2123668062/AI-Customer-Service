"""健康检查与聊天主链路（SSE）的端到端测试。

外部依赖全部 mock：Redis（热层）、LLM 生成、DB 归档、写路径钩子、埋点——
测试不依赖真实服务。主链路顺序断言 / 六档预设 smoke / 埋点字段 / meta 三态等
E12 专属用例在 test_pipeline.py。
"""
import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.services import chat_pipeline
from app.services.chat_pipeline import MemoryContext
from app.services.generation_service import GenerationResult

client = TestClient(app)


def test_health_check_returns_ok():
    """测试 /health 接口是否能正常返回 status: ok"""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def _parse_sse_events(raw: bytes) -> list[dict]:
    """将 SSE 响应体解析为事件列表"""
    text = raw.decode("utf-8")
    events = []
    for line in text.split("\n"):
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


def _extract_full_reply(events: list[dict]) -> str:
    """从 SSE 事件列表中提取完整的 message 内容"""
    return "".join(evt["content"] for evt in events if evt.get("event") == "message")


def test_chat_endpoint_stores_and_replies(mock_redis):
    """测试聊天端点：主链路定稿（锁 → 读取 → 组装 → 生成 → 归档 → 埋点），SSE 流式返回"""
    with patch("app.core.short_term_memory.redis_client", mock_redis), \
            patch("app.services.generation_service.generate_reply", new_callable=AsyncMock) as mock_generate, \
            patch("app.services.chat_pipeline.save_record_to_db", new_callable=AsyncMock, return_value=1), \
            patch("app.services.chat_pipeline._retrieve_memory_context",
                  new_callable=AsyncMock, return_value=MemoryContext()), \
            patch("app.services.chat_pipeline._emit_turn_trace", new_callable=AsyncMock), \
            patch("app.services.chat_pipeline.consolidator.note_activity",
                  new_callable=AsyncMock, return_value=None):
        mock_generate.return_value = GenerationResult(reply="哈哈，测试闲聊回复，包含关键字：测试")

        payload = {"session_id": "test_auto_001", "message": "这是一条自动化测试消息"}
        response = client.post("/api/v1/chat/", json=payload)

    assert response.status_code == 200
    events = _parse_sse_events(response.content)
    assert len(events) >= 2
    assert events[0]["event"] == "status"
    assert events[-1]["event"] == "done"

    full_reply = _extract_full_reply(events)
    assert "测试" in full_reply

    # E12：meta 事件在 status 与 message 之间下发（无注入 → none）
    metas = [e for e in events if e["event"] == "meta"]
    assert len(metas) == 1
    assert metas[0]["content"]["status"] == "none"


def test_chat_no_content_interception(mock_redis):
    """D16：不做任何内容拦截——原客服词（"作弊"）消息照常进入组装包、生成与归档。"""
    with patch("app.core.short_term_memory.redis_client", mock_redis), \
            patch("app.services.generation_service.generate_reply", new_callable=AsyncMock) as mock_generate, \
            patch("app.services.chat_pipeline.save_record_to_db", new_callable=AsyncMock, return_value=1), \
            patch("app.services.chat_pipeline._retrieve_memory_context",
                  new_callable=AsyncMock, return_value=MemoryContext()), \
            patch("app.services.chat_pipeline._emit_turn_trace", new_callable=AsyncMock), \
            patch("app.services.chat_pipeline.consolidator.note_activity",
                  new_callable=AsyncMock, return_value=None):
        mock_generate.return_value = GenerationResult(reply="正常生成：讨论作弊话题不受影响")
        payload = {"session_id": "test_no_intercept_001", "message": "你怎么在这里作弊啊"}
        response = client.post("/api/v1/chat/", json=payload)

    assert response.status_code == 200
    events = _parse_sse_events(response.content)
    full_reply = _extract_full_reply(events)
    assert "正常生成" in full_reply
    mock_generate.assert_awaited()                    # 未被拦截：确实走到了生成
    package = mock_generate.await_args.args[0]        # E12：生成入口收组装包
    assert package.current_input == "你怎么在这里作弊啊"


# ================================
# 记忆接入点接线（E10 交付；E12 适配组装包 + trace 回传）
# ================================

def test_chat_pipeline_injects_memory_context_into_generation(mock_redis):
    """接线验证：注入文本经黑盒口进入组装包（memory_injection 段）。"""
    memory_text = "【记忆参考】以下是关于用户的已知信息，回答时自然结合，勿生硬复述：\n- [事实·当前] 用户对花生严重过敏"
    with patch("app.core.short_term_memory.redis_client", mock_redis), \
            patch("app.services.generation_service.generate_reply", new_callable=AsyncMock) as mock_generate, \
            patch("app.services.chat_pipeline.save_record_to_db", new_callable=AsyncMock, return_value=1), \
            patch("app.services.chat_pipeline._retrieve_memory_context",
                  new_callable=AsyncMock, return_value=MemoryContext(text=memory_text)), \
            patch("app.services.chat_pipeline._emit_turn_trace", new_callable=AsyncMock), \
            patch("app.services.chat_pipeline.consolidator.note_activity",
                  new_callable=AsyncMock, return_value=None):
        mock_generate.return_value = GenerationResult(reply="好的，我会帮你避开花生相关的食物")
        payload = {"session_id": "test_mem_inject_001", "message": "我对什么东西过敏来着？"}
        response = client.post("/api/v1/chat/", json=payload)

    assert response.status_code == 200
    package = mock_generate.await_args.args[0]
    assert package.memory_injection == memory_text     # 注入文本经黑盒口贯通到组装包
    assert package.current_input == "我对什么东西过敏来着？"
    assert isinstance(package.window, list)            # 历史窗口素材随包交付（mock Redis 不存内容）


@pytest.mark.asyncio
async def test_retrieve_memory_context_wires_three_stages(monkeypatch):
    """三段接线（retriever→reranker→injector）与 trace 回传（纯单测，不碰 LLM/DB/Redis）。"""
    from app.services.long_term_memory import reranker, retriever

    calls: dict = {}

    class _FakeSessionCtx:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            return False

    async def fake_retrieve(query, *, session=None, now=None):
        calls["retrieve"] = (query, session)
        return retriever.RetrievalResult(candidates=["C1"], trace={"triage": "fault"})

    async def fake_rerank(query, candidates):
        calls["rerank"] = (query, candidates)
        return reranker.RerankResult(kept=["K1"], trace={"judged": 1})

    def fake_render(memories, *, window_texts=(), trace=None):
        calls["render"] = (memories, list(window_texts))
        if trace is not None:
            trace.update({"injected": ["K1"], "skipped_window": []})
        return "RENDERED"

    monkeypatch.setattr(settings, "RETRIEVAL_MODE", "full")   # memory_read_enabled=True
    monkeypatch.setattr(chat_pipeline, "AsyncSessionLocal", lambda: _FakeSessionCtx())
    monkeypatch.setattr(chat_pipeline.retriever, "retrieve", fake_retrieve)
    monkeypatch.setattr(chat_pipeline.reranker, "rerank", fake_rerank)
    monkeypatch.setattr(chat_pipeline.injector, "render", fake_render)

    out = await chat_pipeline._retrieve_memory_context(
        "s1", "问句", window_texts=["窗口消息一", "窗口消息二"])
    assert out.text == "RENDERED"
    assert calls["retrieve"] == ("问句", None)                        # 拿会话开 DB session
    assert calls["rerank"] == ("问句", ["C1"])                        # 检索候选进精筛
    assert calls["render"] == (["K1"], ["窗口消息一", "窗口消息二"])  # kept + 历史窗口进渲染
    assert out.retrieval_trace == {"triage": "fault"}                 # E12：三段 trace 回传（埋点源）
    assert out.rerank_trace == {"judged": 1}
    assert out.injector_trace["injected"] == ["K1"]
    assert out.kept == ["K1"]


@pytest.mark.asyncio
async def test_retrieve_memory_context_none_when_retrieval_off(monkeypatch):
    """B1/B2/B3 档位（RETRIEVAL_MODE=off）：黑盒口直接空产出，不碰任何依赖。"""
    monkeypatch.setattr(settings, "RETRIEVAL_MODE", "off")
    out = await chat_pipeline._retrieve_memory_context("s1", "问句", window_texts=[])
    assert out.text is None
    assert out.retrieval_trace == {} and out.kept == []
