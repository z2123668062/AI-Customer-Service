from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.endpoints import chat, memory, sessions
from app.core.database import AsyncSessionLocal, init_db
from app.core.error_handlers import register_exception_handlers
from app.core.logging import logger
from app.services.long_term_memory import consolidator, evictor, pool, store


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动：建表（幂等）→ 存储层启动检查 → 快表预热构建（E6）→ 注册写钩子
    → 淘汰巡检（E11）→ 写路径启动补偿 + 空闲收口巡检（E8；D23 journal 自检）。

    顺序纪律：startup_check 先修复（status 漂移 + Qdrant 对账），再让快表按修复后的
    真相预热最近 C 张；构建完成后注册钩子，写轨从此刻起直通快表（D5 第二棒）；
    淘汰巡检在钩子之后——下架卡经写序列自动摘出快表/向量（三面一致）。
    写路径后台（补偿扫描 / 巡检）不阻塞启动、失败只告警。
    """
    await init_db()
    async with AsyncSessionLocal() as session:
        await store.startup_check(session)
        await pool.build(session)
    store.register_pool_hook(pool.on_store_write)
    try:                                     # E11 淘汰：启动巡检（无常驻定时器；消融档位内跳过）
        async with AsyncSessionLocal() as session:
            await evictor.sweep(session)
    except Exception as e:                   # noqa: BLE001 —— 淘汰巡检失败不影响启动
        logger.warning(f"启动淘汰巡检失败（不影响启动）: {e}")
    try:                                     # E8 写路径：启动补偿扫描（journal 自检 + 落后补抽）
        await consolidator.startup_scan()
        consolidator.start_idle_reaper()     # 空闲收口巡检（静默超时会话）
    except Exception as e:                   # noqa: BLE001 —— 写路径后台不影响启动
        logger.warning(f"写路径启动扫描失败（不影响启动）: {e}")
    yield
    await consolidator.stop_idle_reaper()
    await consolidator.wait_idle(timeout=5.0)
    store.register_pool_hook(None)


app = FastAPI(
    title="个人助手（长期记忆系统毕设）",
    description="单用户个人助手：聊天 + 长期记忆系统。记忆系统是本研究的主体；"
                "原客服系统的用户体系、语义路由、RAG 知识库、工具调用已在系统减法中移除。",
    version="0.1.0",
    lifespan=lifespan,
)

# 单用户、无 Cookie 鉴权：通配来源 + 不带凭据。二者不能同时为 True
# （浏览器会拒绝 Access-Control-Allow-Origin: * 与 Allow-Credentials: true 的组合）。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", tags=["基础维护"])
async def health_check():
    """检查应用是否存活"""
    return {"status": "ok"}


# ================================
# 路由挂载（减法后仅剩聊天与记忆管理）
# ================================
app.include_router(
    chat.router,
    prefix="/api/v1/chat",
    tags=["聊天会话"]
)
app.include_router(
    sessions.router,
    prefix="/api/v1/sessions",
    tags=["会话列表"]
)
app.include_router(
    memory.router,
    prefix="/api/v1/memory",
    tags=["记忆管理"]
)


@app.middleware("http")
async def trace_id_middleware(request, call_next):
    import uuid
    request.state.trace_id = uuid.uuid4().hex
    response = await call_next(request)
    return response


register_exception_handlers(app)
