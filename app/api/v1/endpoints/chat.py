import logging
logger = logging.getLogger(__name__)
from fastapi import APIRouter
from app.models.schemas import ChatRequest, ChatResponse
from app.core.memory import add_message, get_history_count
# 【核心新增】：导入咱们刚写好的知识库查询函数
from app.services.rag_service import query_knowledge
from app.services.tool_service import execute_tool_call
from app.services.safety_service import check_input_safety, get_safety_rejection_message
import uuid
from app.services.router_service import analyze_intent, generate_chitchat
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

    trace_id = uuid.uuid4().hex  # 生成类似 8f5b8c9d... 的字符串
    logger.info(f"[Trace: {trace_id}] 收到新会话请求: session={request.session_id}, msg={request.message}")

    #合规检查
    is_safe=await check_input_safety(request.message)
    if not is_safe:
        reject_msg=await get_safety_rejection_message()
        return ChatResponse(session_id=request.session_id,reply=reject_msg,history_count=get_history_count(request.session_id),trace_id=trace_id)

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
    # 【新增调试日志】：直接在控制台打出路由器判断出来的分类结果！
    print(f"\n======================================")
    print(f"🧐 [路由器大脑判定结果] ==>")
    print(f"意图 (Intent): {router_result.intent}")
    print(f"提出关键字 (Keywords): {router_result.keywords}")
    print(f"======================================\n")

    # 3. 根据路由器判断出来的数据编写业务分支
    if router_result.intent == "chitchat":
        # 闲聊处理分支：不再写死，而是真正调用大模型
        reply_content = await generate_chitchat(request.message)


    elif router_result.intent == "kb_qa":
        # 查知识库处理分支
        try:
            # 直接把路由器提取的关键词传给 RAG 服务
            # 如果一切顺利，reply_content 就会是大模型结合文档回复的人话
            reply_content = query_knowledge(request.message)
        except Exception as e:
            # 兜底机制：万一没上传文档，或者 ChromaDB 还没初始化成功，不至于让前端看到 500 报错
            reply_content = f"抱歉，我在翻阅公司知识库时遇到了点问题：{str(e)}"

    elif router_result.intent == "tool":
        # 工具调用处理分支
        reply_content = execute_tool_call(request.message)
    elif router_result.intent == "complaint":
        reply_text="十分抱歉给您带来不便！我已经记录了您的问题并请求转接人工客服，请您稍作等待..."
        return ChatResponse(
            session_id=request.session_id,
            reply=reply_text,
            history_count=get_history_count(request.session_id),
            trace_id=trace_id)

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
        history_count=current_count,
        trace_id=trace_id
    )
