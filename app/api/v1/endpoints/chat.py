from fastapi import APIRouter
from app.models.schemas import ChatRequest, ChatResponse
from app.core.memory import add_message, get_history_count

# 这里创建一个“路由器”，它的作用是把请求分发到对应的函数里
router = APIRouter()


# @router.post 就是告诉 FastAPI：这是一个接收数据（Post）的接口
@router.post("/", response_model=ChatResponse)
async def chat_endpoint(request: ChatRequest):
    """
    接收用户消息，并返回助手的回复。
    第一期我们先做最简单的“固定回显”，先不接大模型。
    """

    # 1. 记住用户刚说的话
    add_message(
        session_id=request.session_id,
        role="user",
        content=request.message
    )

    # 2. 模拟系统思考并生成回复（第一期先回显）
    reply_content = f"我已经收到了你的消息：{request.message}"

    # 3. 记住系统刚刚产生的回复
    add_message(
        session_id=request.session_id,
        role="assistant",
        content=reply_content
    )

    # 4. 获取最新的历史对话条数，打包返回给前端
    current_count = get_history_count(request.session_id)

    return ChatResponse(
        session_id=request.session_id,
        reply=reply_content,
        history_count=current_count
    )
