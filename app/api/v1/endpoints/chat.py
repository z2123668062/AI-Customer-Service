"""HTTP/SSE 适配层：只负责请求解析、SSE 流式包装与日志。

编排逻辑（锁/显式识别/记忆读取/组装包/生成/归档/写路径/埋点）全部在
app/services/chat_pipeline.py。

事件序列（E12 定稿）：status（思考中）→ [meta（"本轮记忆"可视化，可选）]
→ message×N（逐字符）→ done。meta 的 content 为对象（非字符串）。
"""
import asyncio
import json
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.core.logging import logger
from app.models.schemas import ChatRequest
from app.services import chat_pipeline

router = APIRouter()

# 单轮总超时（含记忆读取 + LLM 生成 + 归档）
TURN_TIMEOUT = 30.0


def _sse(event: str, content="") -> str:
    """构造一条 SSE 事件（JSON 序列化，天然转义引号与换行；content 可为对象）。"""
    return f"data: {json.dumps({'event': event, 'content': content}, ensure_ascii=False)}\n\n"


@router.post("/")
async def chat_endpoint(request: ChatRequest, fastapi_request: Request = None):
    trace_id = getattr(fastapi_request.state, "trace_id", uuid.uuid4().hex) if fastapi_request else uuid.uuid4().hex
    logger.info(f"[Trace: {trace_id}] 收到消息: session={request.session_id}, msg={request.message}")

    async def stream_generator():
        yield _sse("status", "正在思考...")
        memory_meta = None
        try:
            outcome = await asyncio.wait_for(
                chat_pipeline.handle_message(request.session_id, request.message, trace_id=trace_id),
                timeout=TURN_TIMEOUT,
            )
            reply = outcome.reply
            memory_meta = outcome.memory_meta
        except asyncio.TimeoutError:
            logger.warning(f"[Trace: {trace_id}] 生成超时")
            reply = "抱歉，我思考得有点久了，请再说一遍试试？"
        except Exception as e:
            logger.error(f"[Trace: {trace_id}] 处理失败: {e}")
            reply = "抱歉，我这边出了点故障，请稍后再试。"

        # meta 先于正文下发（前端据此渲染"本轮记忆"条；异常路径无 meta）
        if memory_meta:
            yield _sse("meta", memory_meta)

        # 逐字符推送（保留原系统的打字机视觉效果）
        for char in reply:
            yield _sse("message", char)
            await asyncio.sleep(0.01)
        yield _sse("done")

    return StreamingResponse(stream_generator(), media_type="text/event-stream")
