"""应用配置：基础设施连接 + 策略参数。

设计原则：**只有一条主干**。
本系统不做运行模式切换——上下文一律是"滑动窗口 + 记忆注入"，写入一律是"卡片"，
检索一律是"缺页回源全链"。因此这里没有模式枚举、没有档位预设、没有旁路开关；
只有会影响运行效果的具体数值（窗口多大、阈值多少、批量几条、半衰期几天）。

同步纪律：改任何默认值 = 同步改 `.env.example`（必要时 `.env`）。
"""
import os

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # ================= 基本信息 =================
    PROJECT_NAME: str = "MemoryAssistant"
    DEBUG_MODE: bool = True

    # ================= 大模型（生成用）=================
    # 没有等号 = "必填项"，.env 里没配程序启动直接报错
    LLM_API_KEY: str
    LLM_BASE_URL: str = "https://api.deepseek.com"
    LLM_MODEL: str = "deepseek-flash"

    # ================= 数据库 =================
    # 开发/演示建议 SQLite（sqlite+aiosqlite:///./data/app.db，不依赖外部服务）
    DATABASE_URL: str

    # ================= 中间件 =================
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0
    API_TIMEOUT: float = 10.0

    # ================= 热层窗口 =================
    # 短期记忆保留的消息条数（一轮 = 用户 + 助手两条）。
    # 产品侧取 20（10 轮）：窗口内的原文是最便宜也最准的上下文，放宽它比多注入卡片更划算。
    # 注意：这是**产品默认值**，评测口径以 run_eval 显式传入的窗口为准；改动后若重跑评测，
    # 需在记录里注明窗口尺寸，否则与历史结果不可比。
    WINDOW_SIZE: int = 20

    # ================= 读路径 · 检索与精筛 =================
    GATE_THRESHOLD: float = 0.35            # 精筛硬阈值：档位折算分低于此值不注入
    # 精筛开关（2026-11-01 消融）：关掉后不做逐卡 LLM 判定，按语义分 + 入口兜底分排序取前 N。
    # 存在的意义是量清楚这道环节值多少钱——它占端到端 token 的九成、延迟的七成。
    RERANK_ENABLED: bool = True
    INJECT_MAX_CARDS: int = 8               # 单轮注入上限（精筛后截断；实测平均丢弃 10 张合格卡）
    SOURCE_SCAN_LIMIT: int = 200            # 回源成本上限：翻查条数（防扫穿全库）
    LINE_PREFETCH_LIMIT: int = 3            # 顺线一跳每卡取邻居数上限
    # 粗排候选预算（工作集上限）：候选超过该数时，按语义分排序截断后再交精筛。
    # 定位纪律：这是**预算**不是筛选——被截断的卡仍在册、仍可经其它入口进入，
    # 只是本轮不再逐张判档（精筛的成本是每张一次 LLM 调用，预算必须显式）。
    # 取值依据：实测 K=20 会截掉 3/44 题该用的卡（语义分区分度不足），K=60 全部恢复；
    # 60 也接近"精筛过门槛张数"的 5 倍，足够覆盖一张卡的邻居扩展。
    RETRIEVAL_COARSE_TOPK: int = 60
    # 原文通道（L8）：卡片是有损抽象，命中路径也把相关原文一并带上送入精筛。
    # 之前这条通道只在"快表全空"时才跑，44 道题只撞上 1 次，等于没有。
    RETRIEVAL_RAW_TOPK: int = 6

    # ================= 判定器 =================
    GATE_MODEL: str | None = None           # 判定模型名；None = 跟随 LLM_MODEL
    GATE_UPGRADE_MODEL: str | None = None   # 主档连续失败时的升级档；None = 不启用
    GATE_TIMEOUT_SECONDS: float = 6.0       # 单次判定调用超时（热路径纪律）
    GATE_CONFIDENCE_MIN: float = 0.5        # 低置信阈值：低于此值按保守方向处理

    # ================= 写路径 · 抽取与水位 =================
    EXTRACT_WATERMARK_ROUNDS: int = 4       # 攒批轮数：待抽消息 ≥ N×2 条即触发
    EXTRACT_IDLE_TIMEOUT_MINUTES: int = 30  # 空闲收口：全局静默超时后补抽
    EXTRACT_MODEL: str | None = None        # 抽取模型名；None = 跟随 LLM_MODEL
    EXTRACT_JOURNAL_PATH: str = "data/extract_journal.log"  # 抽取水位日志（相对 BASE_DIR）

    # ================= 淘汰与编辑层 =================
    EVICT_GOAL_GRACE_DAYS: int = 30         # 目标卡过期宽限：event_time + 本值 < now → 下架
    EVICT_PREF_HALFLIFE_DAYS: int = 90      # 偏好卡半衰期（天）：score = 0.5^(age/HL)；explicit ×2
    EVICT_MIN_SCORE: float = 0.25           # 衰减下架阈值：score < 本值 → evicted
    EVICT_LOG_PATH: str = "data/evict_log.jsonl"  # 淘汰日志（相对 BASE_DIR）
    EDIT_LOG_PATH: str = "data/edit_log.jsonl"    # 编辑审计日志（相对 BASE_DIR）

    # ================= 运行指标 =================
    TELEMETRY_ENABLED: bool = True
    TELEMETRY_DIR: str = "data/telemetry"   # 相对 BASE_DIR；JSONL 按天落盘

    # ================= Qdrant 向量库（唯一封装在 store.py）=================
    QDRANT_URL: str = "http://localhost:6333"
    QDRANT_COLLECTION: str = "memory_cards"
    QDRANT_TIMEOUT: float = 3.0             # 单次调用超时（秒）：写失败快速降级入重试队列
    QDRANT_ENABLED: bool = True             # False = 完全绕过向量面（无 Docker 的开发/纯 CRUD 场景）

    # ================= Embedding =================
    # provider=api（默认）：OpenAI 兼容接口（硅基流动），需 EMBEDDING_API_KEY
    # provider=local（离线兜底）：sentence-transformers 本地模型（torch 栈按需安装）
    EMBEDDING_PROVIDER: str = "api"
    EMBEDDING_MODEL: str = "Qwen/Qwen3-Embedding-8B"
    EMBEDDING_DIM: int = 1024                             # MRL 截断维度（Qwen3 原生 4096 → 1024）
    EMBEDDING_BASE_URL: str = "https://api.siliconflow.cn/v1"
    EMBEDDING_API_KEY: str | None = None

    # 获取 app 文件夹的上一级（即项目根目录），供 .env 定位与日志目录使用
    BASE_DIR: str = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    model_config = SettingsConfigDict(
        env_file=os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".env"),
        env_file_encoding="utf-8",
        extra="ignore"
    )


# 实例化对象，一旦实例化，它就会立即执行上述所有的校验动作
settings = Settings()
