from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MR_DATA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"

    postgres_dsn: str = "postgresql://user:password@localhost:5432/mrdata"

    chroma_persist_dir: str = "./data/chroma"

    # 离线任务参数
    offline_batch_size: int = 50
    offline_lookback_days: int = 7
    failure_threshold: int = 5


settings = Settings()
