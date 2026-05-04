from fastapi import APIRouter
from app.models.schemas import ChatRequest, ChatResponse
from app.core.memory import add_message, get_history_count
from app.services.router_service import analyze_intent
# 【核心新增】：导入咱们刚写好的知识库查询函数
from app.services.rag_service import query_knowledge
from app.services.tool_service import execute_tool_call

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
    # 【新增调试日志】：直接在控制台打出路由器判断出来的分类结果！
    print(f"\n======================================")
    print(f"🧐 [路由器大脑判定结果] ==>")
    print(f"意图 (Intent): {router_result.intent}")
    print(f"提出关键字 (Keywords): {router_result.keywords}")
    print(f"======================================\n")

    # 3. 根据路由器判断出来的数据（此时它已经是我们的实体对象）编写业务分支
    if router_result.intent == "chitchat":
        # 闲聊处理分支（目前我们还是写死回复，后面会专门做闲聊生成模块）
        reply_content = f"系统判定你正在：闲聊。检测到的关键词是：{router_result.keywords}"


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
