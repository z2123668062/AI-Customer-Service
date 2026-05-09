import logging
from fastapi import Request, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi import FastAPI
from app.api.v1.endpoints import chat
from fastapi.responses import JSONResponse
app=FastAPI(
    title="智路由AI客服系统",
    description="一个基于 FastAPI 和 LLaMAIndex 的智能客服系统，支持闲聊和知识库问答两大功能模块。前端使用 React 实现，后端集成了智谱的免费大语言模型和本地的 HuggingFace 向量模型，能够高效地处理用户查询并提供智能回复。",
    version="0.5.0"
)

@app.get("/health",tags=["基础维护"])
async def health_check():
    """检查应用是否存活"""
    return {"status": "ok"}

# ================================
# 核心操作：把 chat 的路由挂载进来
# ================================
# 意思是：凡是访问 "/api/v1/chat" 开头的请求，都丢给 chat.router 去办
app.include_router(
    chat.router,
    prefix="/api/v1/chat",
    tags=["聊天会话"]
)



logger = logging.getLogger(__name__)

# ================================
# 1. 对应 SpringBoot：参数校验校验失败拦截 (RequestValidationError)
# ================================
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """当前端传来的参数不符合 Pydantic 模型 (比如没传 session_id) 时触发"""
    logger.warning(f"参数校验失败，请求：{request.url}，错误信息：{exc.errors()}")
    # 返回标准的响应格式
    return JSONResponse(
        status_code=422,
        content={
            "session_id": "unknown",
            "reply": "请求格式有误，我看不太懂，请检查您的输入格式哦。",
            "error_detail": exc.errors()  # 仅供调试看
        }
    )

# ================================
# 2. 对应 SpringBoot：HTTP / 业务级异常拦截 (HTTPException)
# ================================
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """当开发者在代码中主动 raise HTTPException (如 404，401) 时触发"""
    logger.error(f"业务逻辑 HTTP 异常，请求：{request.url}，状态码：{exc.status_code}")
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "session_id": "unknown",
            "reply": f"系统提示：{exc.detail}",
            "error_detail": exc.detail
        }
    )

# ================================
# 3. 对应 SpringBoot：系统未知异常兜底 (Exception)
# ================================
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """最高层级的兜底，应对如第三方 API 崩溃、空指针、数据库宕机等"""
    logger.critical(f"系统发生未捕获的严重异常！请求：{request.url}，错误：{str(exc)}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "session_id": "unknown",
            "reply": "此刻人工客服全线爆满，AI 系统也因为拥挤暂时掉线了。请喝口水稍后再试...",
            "error_detail": str(exc) # 生产环境为了安全可以屏蔽这条消息给前端
        }
    )