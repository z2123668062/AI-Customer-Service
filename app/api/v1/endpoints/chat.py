from app.core.logging import logger
from fastapi import APIRouter,BackgroundTasks,HTTPException
from app.models.schemas import ChatRequest, ChatResponse
from app.core.memory import add_message, get_history_count
# 【核心新增】：导入咱们刚写好的知识库查询函数
from app.services.rag_service import query_knowledge
from app.services.tool_service import execute_tool_call
from app.services.safety_service import check_input_safety, get_safety_rejection_message
import uuid
from app.services.router_service import analyze_intent, generate_chitchat
from app.core.memory import get_history,redis_client
from app.services.history_service import save_record_to_db
from fastapi.responses import StreamingResponse
import asyncio
# 这里创建一个“路由器”，它的作用是把请求分发到对应的函数里
router = APIRouter()


# @router.post 就是告诉 FastAPI：这是一个接收数据（Post）的接口
@router.post("/")
async def chat_endpoint(request: ChatRequest,background_tasks: BackgroundTasks):
    """
    接收用户消息，并返回助手的回复。
    第一期我们先做最简单的“固定回显”，先不接大模型。
    现在第二期，我们用智谱的免费模型判断一下类型。
    现在第三期，我们加入redis锁。
    """

    trace_id = uuid.uuid4().hex  # 生成类似 8f5b8c9d... 的字符串
    logger.info(f"[Trace: {trace_id}] 收到新会话请求: session={request.session_id}, msg={request.message}")

    lock_key = f"lock:order:{request.session_id}"
    lock = redis_client.lock(name=lock_key, timeout=5)
    acquired = lock.acquire(blocking=False)
    if not acquired:
        logger.warning(f"[Trace: {trace_id}] 会话 {request.session_id} 的请求过于频繁，已被锁定。")
        raise HTTPException(
            status_code=409,
            detail="系统正在处理您的上一条消息，请不要频繁点击哦~"
        )

    try:
        is_safe = await check_input_safety(request.message)
        if not is_safe:
            reject_msg = await get_safety_rejection_message()
            try:
                lock.release()
            except Exception:
                pass
            return ChatResponse(session_id=request.session_id, reply=reject_msg,
                                history_count=get_history_count(request.session_id), trace_id=trace_id)

        add_message(request.session_id, role="user", content=request.message)

        router_result = await analyze_intent(request.message)
        logger.info(f"[Trace: {trace_id}] 语义路由结果: intent={router_result.intent}, keywords={router_result.keywords}")

        async def stream_generator():
            try:
                yield f"data: {{\"event\": \"status\", \"content\": \"正在处理您的[{router_result.intent}]请求...\"}}\n\n"

                final_full_reply = ""

                if router_result.intent == "chitchat":
                    history_from_redis = get_history(request.session_id)
                    reply_content = await generate_chitchat(history_from_redis)

                    for char in reply_content:
                        yield f"data: {{\"event\": \"message\", \"content\": \"{char}\"}}\n\n"
                        final_full_reply += char
                        await asyncio.sleep(0.01)

                elif router_result.intent == "kb_qa":
                    try:
                        reply_content = query_knowledge(request.message)
                        for char in reply_content:
                            yield f"data: {{\"event\": \"message\", \"content\": \"{char}\"}}\n\n"
                            final_full_reply += char
                            await asyncio.sleep(0.02)
                    except Exception as e:
                        error_msg = f"抱歉，知识库遇见故障：{str(e)}"
                        yield f"data: {{\"event\": \"error\", \"content\": \"{error_msg}\"}}\n\n"
                        final_full_reply = error_msg

                elif router_result.intent == "tool":
                    reply_content = await execute_tool_call(request.message)
                    for char in reply_content:
                        yield f"data: {{\"event\": \"message\", \"content\": \"{char}\"}}\n\n"
                        final_full_reply += char
                        await asyncio.sleep(0.01)

                elif router_result.intent == "complaint":
                    reply_text = "十分抱歉给您带来不便！我已经记录了您的问题并请求转接人工客服，请您稍作等待..."
                    for char in reply_text:
                        yield f"data: {{\"event\": \"message\", \"content\": \"{char}\"}}\n\n"
                        final_full_reply += char
                        await asyncio.sleep(0.05)
                else:
                    fallback_msg = "我不太明白你的意思。"
                    for char in fallback_msg:
                        yield f"data: {{\"event\": \"message\", \"content\": \"{char}\"}}\n\n"
                        final_full_reply += char
                        await asyncio.sleep(0.01)

                yield f"data: {{\"event\": \"done\", \"content\": \"\"}}\n\n"

                add_message(request.session_id, role="assistant", content=final_full_reply)
                background_tasks.add_task(save_record_to_db, request.session_id, "user", request.message)
                background_tasks.add_task(save_record_to_db, request.session_id, "assistant", final_full_reply)

            finally:
                try:
                    lock.release()
                except Exception:
                    pass

        return StreamingResponse(stream_generator(), media_type="text/event-stream")

    except Exception:
        try:
            lock.release()
        except Exception:
            pass
        raise