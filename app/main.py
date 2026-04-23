from fastapi import FastAPI
from app.api.v1.endpoints import chat
app=FastAPI(
    title="智路由AI客服系统",
    description="第一期最小联调测试版本",
    version="0.1.0"
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