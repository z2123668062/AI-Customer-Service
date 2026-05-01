from fastapi import APIRouter
from app.models.schemas import ChatRequest, ChatResponse
from app.core.memory import add_message, get_history_count
from app.services.router_service import analyze_intent


# 这里创建一个“路由器”，它的作用是把请求分发到对应的函数里
router = APIRouter()


# @router.post 就是告诉 FastAPI：这是一个接收数据（Post）的接口
@router.post("/", response_model=ChatResponse)
async def chat_endpoint(request: ChatRequest):
    """
    接收用户消息，并返回助手的回复。
    第一期我们先做最简单的“固定回显”，先不接大模型。
    现在第二期，我们用智谱的免费模型判断一下类型
    """

    # 1. 记住用户刚说的话
    add_message(
        session_id=request.session_id,
        role="user",
        content=request.message
    )

    # 2. 【核心新增：让大模型判断意图】
    # 这一步有点像 SpringAOP 里的前置过滤，或者网关（Gateway）里的路由判定
    # await 的意思是挂起，等待远程智谱或者DeepSeek那边的服务器把JSON结果返回给我们
    router_result = await analyze_intent(request.message)

    # 3. 根据路由器判断出来的数据（此时它已经是我们的实体对象）编写业务分支
    if router_result.intent == "chitchat":
        # 闲聊处理分支（目前我们还是写死回复，后面会专门做闲聊生成模块）
        reply_content = f"系统判定你正在：闲聊。检测到的关键词是：{router_result.keywords}"

    elif router_result.intent == "kb_qa":
        # 查知识库处理分支
        reply_content = f"系统判定你要查：知识库。我准备拿着关键词 {router_result.keywords} 去数据库搜寻答案。"

    elif router_result.intent == "tool":
        # 工具调用处理分支
        reply_content = f"系统判定你想要：调工具。你需要我帮你用外部工具操作这些：{router_result.keywords}。"

    else:
        # 兜底的异常判断处理
        reply_content = "我不太明白你的意思。"

    # 4. 记住系统刚刚产生的回复
    add_message(
        session_id=request.session_id,
        role="assistant",
        content=reply_content
    )

    # 5. 获取最新的历史对话条数，打包返回给前端
    current_count = get_history_count(request.session_id)

    return ChatResponse(
        session_id=request.session_id,
        reply=reply_content,
        history_count=current_count
    )
