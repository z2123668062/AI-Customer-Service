from fastapi import FastAPI
from app.api.v1.endpoints import chat, kb, auth, sessions
from fastapi.middleware.cors import CORSMiddleware
from app.core.error_handlers import register_exception_handlers

app=FastAPI(
    title="智路由AI客服系统",
    description="一个基于 FastAPI 和 LLaMAIndex 的智能客服系统，支持闲聊和知识库问答两大功能模块。前端使用 React 实现，后端集成了智谱的免费大语言模型和本地的 HuggingFace 向量模型，能够高效地处理用户查询并提供智能回复。",
    version="0.5.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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
app.include_router(
    kb.router,
    prefix="/api/v1/kb",
    tags=["知识库管理"]
)
app.include_router(
    auth.router,
    prefix="/api/v1/auth",
    tags=["用户认证"]
)
app.include_router(
    sessions.router,
    prefix="/api/v1/sessions",
    tags=["会话管理"]
)


@app.middleware("http")
async def trace_id_middleware(request, call_next):
    import uuid
    request.state.trace_id = uuid.uuid4().hex
    response = await call_next(request)
    return response


register_exception_handlers(app)