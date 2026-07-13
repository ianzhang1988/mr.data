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

    postgres_dsn: str = ""  # 留空且 use_pgembed=true 时使用嵌入式 PostgreSQL

    # pgembed 配置（未设置外部 DSN 时的默认运行方式）
    use_pgembed: bool = True
    pgembed_data_dir: str = "./data/pgembed"

    chroma_persist_dir: str = "./data/chroma"

    # 网络搜索 RAG 配置
    enable_web_search: bool = True
    web_search_max_results: int = 3

    # 离线任务参数
    offline_batch_size: int = 50
    offline_lookback_days: int = 7
    failure_threshold: int = 5


settings = Settings()
