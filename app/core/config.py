from pydantic_settings import BaseSettings, SettingsConfigDict
import os

class Settings(BaseSettings):
    # ================= 基本信息 =================
    PROJECT_NAME: str = "AI_Agent_V2"
    DEBUG_MODE: bool = True  # Pydantic会自动把 .env 里的 "True"/"1"/"true" 转成布尔值

    # ================= API 密钥 =================
    # 后面的没有等号，说明这是"必填项"，如果在 .env 里没配，程序启动直接报错
    ZHIPU_API_KEY: str
    DATABASE_URL: str
    JWT_SECRET_KEY: str
    GAODE_WEATHER_KEY: str = ""
    ADMIN_TOKEN: str

    # ================= 中间件连接配置 =================
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0

    # ================= 运行参数 =================
    # 给定了一个默认值，如果在 .env 里不写，就默认用 3 和 10.0
    MAX_RETRY_COUNT: int = 3
    API_TIMEOUT: float = 10.0
    DB_CONNECT_RETRIES: int

    # 获取当前 config.py 文件所在的文件夹的上一级（即 app 文件夹）的上一级（即根目录 E:\AI_Agent）
    # 这样无论你在哪里运行任何测试脚本，系统都能精确定位到根目录的 .env
    BASE_DIR: str = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    # 将绝对路径拼接到 env_file 中
    model_config = SettingsConfigDict(
        env_file=os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".env"),
        env_file_encoding="utf-8",
        extra="ignore"
    )


# 实例化对象，一旦实例化，它就会立即执行上述所有的校验动作
settings = Settings()