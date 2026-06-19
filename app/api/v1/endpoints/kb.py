from fastapi import APIRouter, HTTPException, Header
from app.core.config import settings
from app.services.rag_service import build_knowledge_base

# 创建知识库专用的路由器
router = APIRouter()


@router.post("/build")
async def trigger_build(admin_token: str = Header(..., alias="X-Admin-Token")):
    """
    管理员专用接口（冷任务）：
    扫描后台指定的文件夹，让本地模型计算所有文字，写入数据库持久化。
    """
    if admin_token != settings.ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="无效的管理员令牌")
    # 直接调用刚刚分离出来的单独"做饭（建库）"函数
    success = build_knowledge_base()

    if success:
        return {"message": "知识库离线构建成功！当前知识库已就绪，用户提问将极速响应。"}
    else:
        return {"message": "由于某些原因，构建失败。"}