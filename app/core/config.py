"""应用配置：基础项 + 消融开关矩阵（E3 交付）。

权威来源：docs/实验方案.md §2（B1~B5 矩阵）/ docs/工程规范.md §5（策略集中开关）/
docs/配置与埋点文档.md（字段字典与使用说明）。

分层设计：
- 基础项：LLM / 数据库 / Redis / Qdrant（store.py 已接入）· Embedding（E5 定稿，实验期冻结）；
- 开关矩阵：两个主旋钮（CONTEXT_MODE / RETRIEVAL_MODE）+ 写侧与治理开关 + 策略参数；
- B 预设：PRESET=B1~B5 一键展开成整组开关（消融 = 切配置预设，禁止改代码）；
- 优先级：代码默认值 < 预设展开 < .env / 环境变量显式覆盖（单因子实验可只覆盖一项）。

同步纪律（E3 起）：改任何默认值 = 同步改 `.env.example`（必要时 `.env`），三处一致。

退役说明：旧总开关 MEMORY_READ_ENABLED 已并入矩阵——读开关见 `memory_read_enabled`
属性（= RETRIEVAL_MODE != "off"；B1/B2/B3 档位即为关）。
"""
import hashlib
import json
import os
from typing import Any

from pydantic import ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# ============ 取值枚举（全仓库引用这里，勿散写字符串）============
CONTEXT_MODES = ("full_history", "window", "window+memory")
WRITE_MODES = ("off", "raw_chunks", "cards")
RETRIEVAL_MODES = ("off", "flat_raw", "flat", "full")

# 抽取 prompt 版本（E8）：改 prompt 必须改版本号（换 prompt = 换实验条件，随快照登记）
EXTRACT_PROMPT_VERSION = "v1"

# ============ B1~B5 预设（权威：docs/实验方案.md §2 矩阵表，逐列照抄）============
# 列对应：context.mode / 抽取(write) / retrieval.mode / 取代+淘汰 / 门控
# B4′ 的环境变量安全写法为 B4P（"B4′" 别名会自动归一）。
ABLATION_PRESETS: dict[str, dict[str, Any]] = {
    "B1": dict(CONTEXT_MODE="full_history", WRITE_MODE="off", RETRIEVAL_MODE="off",
               SUPERSEDE_ENABLED=False, EVICT_ENABLED=False, GATE_ENABLED=False),
    "B2": dict(CONTEXT_MODE="window", WRITE_MODE="off", RETRIEVAL_MODE="off",
               SUPERSEDE_ENABLED=False, EVICT_ENABLED=False, GATE_ENABLED=False),
    "B3": dict(CONTEXT_MODE="window", WRITE_MODE="cards", RETRIEVAL_MODE="off",
               SUPERSEDE_ENABLED=False, EVICT_ENABLED=False, GATE_ENABLED=False),
    "B4P": dict(CONTEXT_MODE="window+memory", WRITE_MODE="raw_chunks", RETRIEVAL_MODE="flat_raw",
                SUPERSEDE_ENABLED=False, EVICT_ENABLED=False, GATE_ENABLED=False),
    "B4": dict(CONTEXT_MODE="window+memory", WRITE_MODE="cards", RETRIEVAL_MODE="flat",
               SUPERSEDE_ENABLED=False, EVICT_ENABLED=False, GATE_ENABLED=False),
    "B5": dict(CONTEXT_MODE="window+memory", WRITE_MODE="cards", RETRIEVAL_MODE="full",
               SUPERSEDE_ENABLED=True, EVICT_ENABLED=True, GATE_ENABLED=True),
}
_PRESET_ALIASES = {"B4′": "B4P"}
_SWITCH_FIELDS = ("CONTEXT_MODE", "WRITE_MODE", "RETRIEVAL_MODE",
                  "SUPERSEDE_ENABLED", "EVICT_ENABLED", "GATE_ENABLED")
_MODE_ENUMS = {"CONTEXT_MODE": CONTEXT_MODES, "WRITE_MODE": WRITE_MODES,
               "RETRIEVAL_MODE": RETRIEVAL_MODES}


class Settings(BaseSettings):
    # ================= 基本信息 =================
    PROJECT_NAME: str = "MemoryAssistant"
    DEBUG_MODE: bool = True  # Pydantic会自动把 .env 里的 "True"/"1"/"true" 转成布尔值

    # ================= 大模型接入（生成用）=================
    # 实验期冻结：换模型 = 换实验条件（随 experiment_snapshot() 登记进快照）
    # 没有等号 = "必填项"，.env 里没配程序启动直接报错
    LLM_API_KEY: str
    LLM_BASE_URL: str = "https://api.deepseek.com"
    LLM_MODEL: str = "deepseek-flash"

    # ================= 数据库 =================
    # 开发/评测建议 SQLite（sqlite+aiosqlite:///./data/app.db，不依赖外部服务）
    # 演示可切 MySQL（mysql+aiomysql://...），见 .env.example
    DATABASE_URL: str

    # ================= 中间件连接配置 =================
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0

    # ================= 运行参数 =================
    MAX_RETRY_COUNT: int = 3
    API_TIMEOUT: float = 10.0
    DB_CONNECT_RETRIES: int = 3

    # ================= 消融开关矩阵（E3 起；权威矩阵见 docs/实验方案.md §2）=================
    # 消融 = 切 PRESET，或用下方单开关做 H2/H3 单因子实验；禁止为消融改代码。
    PRESET: str = "B5"                     # B1 / B2 / B3 / B4P（=B4′）/ B4 / B5
    CONTEXT_MODE: str = "window+memory"    # full_history / window / window+memory
    WRITE_MODE: str = "cards"              # off / raw_chunks（B4′ 原文切块）/ cards
    RETRIEVAL_MODE: str = "full"           # off / flat_raw / flat / full（三入口+缺页+顺线）
    SUPERSEDE_ENABLED: bool = True         # 取代裁决（矩阵"取代/淘汰"列）
    EVICT_ENABLED: bool = True             # 语义淘汰（E11；矩阵"取代/淘汰"列）
    GATE_ENABLED: bool = True              # 精筛门控 + 不召回档位（矩阵"门控"列）

    # ================= 策略参数（当前值；变更 = 换实验条件，随快照登记）=================
    WINDOW_SIZE: int = 10                   # 短记忆窗口消息数（B2 基线形态，同旧 SLIDING_WINDOW_SIZE）
    FULL_HISTORY_MAX_MESSAGES: int = 200    # B1 截断上限（截断策略须如实报告，不能悄悄丢）
    GATE_THRESHOLD: float = 0.35            # 精筛硬阈值（不召回下限；E10 折算门——≥mid 档放行）
    EXTRACT_WATERMARK_ROUNDS: int = 4       # 抽取水位 N（E8；纪律：N ≤ 窗口覆盖轮数）
    EXTRACT_IDLE_TIMEOUT_MINUTES: int = 30  # 空闲超时收口上一会话（E8）
    EXTRACT_MODEL: str | None = None        # 抽取模型名（E8；None = 跟随 LLM_MODEL；实验期冻结）
    EXTRACT_JOURNAL_PATH: str = "data/extract_journal.log"  # 抽取进度日志（D23 全局水位真相；
    #   相对路径基于 BASE_DIR；不进实验快照——它是运行状态载体、不是实验条件）
    RAW_CHUNK_SIZE: int = 200               # B4′ 原文切块粒度（字符；E8）
    LINE_PREFETCH_LIMIT: int = 3            # 顺线一跳每线限量（E9，设计值 2~3）
    INJECT_MAX_CARDS: int = 3               # 精筛后注入上限（E10 截断，设计值 2~3）
    SOURCE_SCAN_LIMIT: int = 200            # 回源成本上限：翻查条数（E9，防扫穿全库）

    # ================= 淘汰与编辑层（E11；evictor 消费）=================
    # 确定性规则参数：零 LLM、可复现；触发 = 启动巡检 / 面板手动 / 评测调用（无常驻定时器）
    EVICT_GOAL_GRACE_DAYS: int = 30         # 目标卡过期宽限：event_time + 本值 < now → 下架
    EVICT_PREF_HALFLIFE_DAYS: int = 90      # 偏好卡半衰期（天）：score = 0.5^(age/HL)；explicit ×2
    EVICT_MIN_SCORE: float = 0.25           # 衰减下架阈值：score < 本值 → evicted
    EVICT_LOG_PATH: str = "data/evict_log.jsonl"  # 淘汰日志（fail-open JSONL；
    #   相对路径基于 BASE_DIR；不进实验快照——运行状态载体、不是实验条件）
    EDIT_LOG_PATH: str = "data/edit_log.jsonl"    # 编辑审计日志（同上；E11 面板操作用）

    # ================= 判定器（E7 交付；约束模式定案 D19，gate.py 消费）=================
    # 实验期冻结：换判定模型 = 换实验条件（随 experiment_snapshot() 登记）。
    GATE_MODEL: str | None = None            # 判定模型名；None = 跟随 LLM_MODEL
    GATE_UPGRADE_MODEL: str | None = None    # 更强模型升级档（D19 可选档；None = 不启用）
    GATE_TIMEOUT_SECONDS: float = 6.0        # 单次判定调用超时（热路径纪律；E10 校准）
    GATE_CONFIDENCE_MIN: float = 0.5         # 低置信阈值：低于此按保守方向处理（E10 校准）
    GATE_PREFILTER_ENABLED: bool = True      # 确定性前置（高置信正则短路；仅缺页判定）

    # ================= 埋点（E3）=================
    TELEMETRY_ENABLED: bool = True
    TELEMETRY_DIR: str = "data/telemetry"   # 相对路径基于 BASE_DIR；JSONL 按天落盘

    # ================= Qdrant 向量库（E5 已接入；唯一封装在 store.py）=================
    QDRANT_URL: str = "http://localhost:6333"
    QDRANT_COLLECTION: str = "memory_cards"
    QDRANT_TIMEOUT: float = 3.0             # 单次调用超时（秒）：写失败快速降级入重试队列
    QDRANT_ENABLED: bool = True             # False = 完全绕过向量面（无 Docker 的开发/纯 CRUD 场景）

    # ================= Embedding（E5 定稿；实验期冻结，D17）=================
    # provider=api（默认，质量优先）：OpenAI 兼容接口（硅基流动），需 EMBEDDING_API_KEY
    # provider=local（离线兜底）：sentence-transformers 本地模型（torch 栈按需安装）
    EMBEDDING_PROVIDER: str = "api"                       # api / local
    EMBEDDING_MODEL: str = "Qwen/Qwen3-Embedding-8B"      # local 时建议 Qwen/Qwen3-Embedding-0.6B
    EMBEDDING_DIM: int = 1024                             # MRL 截断维度（Qwen3 原生 4096 → 截断 1024）
    EMBEDDING_BASE_URL: str = "https://api.siliconflow.cn/v1"
    EMBEDDING_API_KEY: str | None = None                  # 未配置时测试/CRUD 不受影响，仅真嵌入时报错

    # 获取 app 文件夹的上一级（即项目根目录），供 .env 定位与日志目录使用
    BASE_DIR: str = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    # 将绝对路径拼接到 env_file 中
    model_config = SettingsConfigDict(
        env_file=os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".env"),
        env_file_encoding="utf-8",
        extra="ignore"
    )

    # ================= 校验与预设展开 =================
    @field_validator("PRESET")
    @classmethod
    def _normalize_preset(cls, v: str) -> str:
        v = _PRESET_ALIASES.get(v.strip(), v.strip()).upper()
        v = _PRESET_ALIASES.get(v, v)
        if v not in ABLATION_PRESETS:
            raise ValueError(
                f"未知预设 {v!r}；可选：{sorted(ABLATION_PRESETS)}（B4′ 请写 B4P）"
            )
        return v

    @field_validator("CONTEXT_MODE", "WRITE_MODE", "RETRIEVAL_MODE")
    @classmethod
    def _validate_mode(cls, v: str, info: ValidationInfo) -> str:
        v = v.strip().lower()
        allowed = _MODE_ENUMS[info.field_name]
        if v not in allowed:
            raise ValueError(f"{info.field_name}={v!r} 非法；可选：{allowed}")
        return v

    @model_validator(mode="after")
    def _expand_preset(self):
        """预设展开：只填充"未被显式提供"的开关（显式 = env / .env / 构造参数）。"""
        explicit = set(self.model_fields_set) | {f for f in _SWITCH_FIELDS if f in os.environ}
        for field, value in ABLATION_PRESETS[self.PRESET].items():
            if field not in explicit:
                setattr(self, field, value)
        return self

    # ================= 派生开关（旧接口语义出口）=================
    @property
    def memory_read_enabled(self) -> bool:
        """读路径总开关：RETRIEVAL_MODE != "off"（B1/B2/B3 为 False）。"""
        return self.RETRIEVAL_MODE != "off"

    @property
    def memory_write_enabled(self) -> bool:
        """写路径总开关：WRITE_MODE != "off"（B1/B2 为 False；B3 为 True 即"存而不用"）。"""
        return self.WRITE_MODE != "off"

    # ================= 实验快照（D10：配置与模型版本绑定进报告）=================
    def experiment_snapshot(self) -> dict[str, Any]:
        """导出当前实验条件快照（含模型名与全部策略开关/参数）。"""
        return {
            "preset": self.PRESET,
            "model": self.LLM_MODEL,
            "embedding_provider": self.EMBEDDING_PROVIDER,
            "embedding_model": self.EMBEDDING_MODEL,
            "embedding_dim": self.EMBEDDING_DIM,
            "context_mode": self.CONTEXT_MODE,
            "write_mode": self.WRITE_MODE,
            "retrieval_mode": self.RETRIEVAL_MODE,
            "supersede_enabled": self.SUPERSEDE_ENABLED,
            "evict_enabled": self.EVICT_ENABLED,
            "evict_goal_grace_days": self.EVICT_GOAL_GRACE_DAYS,
            "evict_pref_halflife_days": self.EVICT_PREF_HALFLIFE_DAYS,
            "evict_min_score": self.EVICT_MIN_SCORE,
            "gate_enabled": self.GATE_ENABLED,
            "gate_threshold": self.GATE_THRESHOLD,
            "gate_model": self.GATE_MODEL or self.LLM_MODEL,
            "gate_upgrade_model": self.GATE_UPGRADE_MODEL,
            "gate_prefilter_enabled": self.GATE_PREFILTER_ENABLED,
            "gate_confidence_min": self.GATE_CONFIDENCE_MIN,
            "gate_timeout_seconds": self.GATE_TIMEOUT_SECONDS,
            "window_size": self.WINDOW_SIZE,
            "full_history_max_messages": self.FULL_HISTORY_MAX_MESSAGES,
            "extract_watermark_rounds": self.EXTRACT_WATERMARK_ROUNDS,
            "extract_idle_timeout_minutes": self.EXTRACT_IDLE_TIMEOUT_MINUTES,
            "extract_model": self.EXTRACT_MODEL or self.LLM_MODEL,
            "raw_chunk_size": self.RAW_CHUNK_SIZE,
            "extract_prompt_version": EXTRACT_PROMPT_VERSION,
            "line_prefetch_limit": self.LINE_PREFETCH_LIMIT,
            "inject_max_cards": self.INJECT_MAX_CARDS,
            "source_scan_limit": self.SOURCE_SCAN_LIMIT,
        }

    def snapshot_hash(self) -> str:
        """快照指纹（16 位十六进制）：同条件同哈希，供埋点分组与实验登记。"""
        payload = json.dumps(self.experiment_snapshot(), sort_keys=True,
                             ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# 实例化对象，一旦实例化，它就会立即执行上述所有的校验动作
settings = Settings()
