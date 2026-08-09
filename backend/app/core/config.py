"""应用配置（pydantic-settings 从 .env 读取）"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    APP_ENV: str = "dev"
    DATABASE_URL: str = "postgresql+psycopg2://novel:novel@localhost:5432/novel"
    REDIS_URL: str = "redis://localhost:6379/0"
    QDRANT_URL: str = "http://localhost:6333"

    DEEPSEEK_API_KEY: str = ""
    TENCENT_SECRET_ID: str = ""
    TENCENT_SECRET_KEY: str = ""
    TENCENT_REGION: str = "ap-beijing"
    TENCENT_COS_BUCKET: str = ""

    # 生成模型路由
    LLM_MODEL_OUTLINE: str = "deepseek-pro"   # 大纲用 Pro
    LLM_MODEL_CHAPTER: str = "deepseek-flash"  # 正文用 Flash
    LLM_MODEL_REVIEW: str = "deepseek-flash"   # 软审核/摘要


settings = Settings()
