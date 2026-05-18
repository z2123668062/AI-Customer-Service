import os
import sys
from loguru import logger
from app.core.config import settings

# 1. 先让 loguru 忘掉默认的打印方式，我们要重新定制它的长相
logger.remove()

# 2. 定制控制台打印：带颜色、带时间戳、指明是哪个模块在打印
# <green> 时间 </green> | <level> 级别 </level> | <cyan> 模块 </cyan> : <level> 内容 </level>
logger.add(
    sys.stdout,
    format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
    level="DEBUG" if settings.DEBUG_MODE else "INFO"
)

# 3. 核心大招：自动把日志存进文件，永远不爆硬盘规则
# 获取根目录，在根目录下建一个 logs 文件夹用来放日志
log_path = os.path.join(settings.BASE_DIR, "logs")
if not os.path.exists(log_path):
    os.makedirs(log_path)

logger.add(
    os.path.join(log_path, "ai_agent_{time:YYYY-MM-DD}.log"),  # 文件名每天变
    rotation="00:00",      # 每天凌晨 00:00 准时把旧的切割打包，重新开一个新文件
    retention="10 days",   # 最多保留 10 天的冷数据，超过自动删
    compression="zip",     # 历史日志自动达成 zip 压缩包，省空间
    level="INFO",          # 保存进文件里的起步级别是 INFO
    encoding="utf-8",
    enqueue=True           # 开启队列保护，如果是异步高并发，防止多个进程抢着写同一个文件把文件写坏
)

# 4. 把准备好的大炮暴露出去，供别人使用
# 此后其他文件都不用导入 loguru，直接 `from app.core.logging import logger`