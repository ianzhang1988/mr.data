from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _find_project_root() -> Path:
    """从本文件向上逐级探测含 pyproject.toml 的目录，作为项目根。

    找不到时回退到本包文件的固定位置（src/mr_data/config.py 的上三级目录）。
    不使用 Path.cwd()——CWD 是运行目录，仍会漂移。
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    return Path(__file__).resolve().parents[2]


_PROJECT_ROOT = _find_project_root()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MR_DATA_",
        env_file=_PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    # 结构化输出模式：parse（仅原生解析）/ prompt（仅 schema-in-prompt）/ auto（parse 失败后自动降级）
    llm_structured_mode: str = "auto"

    postgres_dsn: str = ""  # 留空且 use_pgembed=true 时使用嵌入式 PostgreSQL

    # 数据根目录（默认 <项目根>/data）。路径默认值锚定项目根；
    # 显式设置的相对路径仍相对当前工作目录（CWD）。
    data_dir: str | None = None

    # pgembed 配置（未设置外部 DSN 时的默认运行方式）
    use_pgembed: bool = True
    pgembed_data_dir: str | None = None  # 默认 <data_dir>/pgembed

    chroma_persist_dir: str | None = None  # 默认 <data_dir>/chroma

    # 人格文件配置（默认 <data_dir>/personalities/data.json）
    personality_file: str | None = None

    # 向量库 embedding 配置
    personality_embedding_model: str = "nomic-ai/nomic-embed-text-v1.5"
    personality_embedding_dim: int = 512
    memory_embedding_model: str = "BAAI/bge-base-zh-v1.5"
    memory_embedding_dim: int = 768
    chroma_recreate_on_mismatch: bool = True

    # 网络搜索 RAG 配置
    enable_web_search: bool = True
    web_search_max_results: int = 3
    web_search_providers: list[str] = ["duckduckgo"]
    searxng_base_url: str = ""
    brave_api_key: str = ""
    bing_api_key: str = ""
    google_api_key: str = ""
    google_cse_id: str = ""

    # 日志配置（log_dir 默认 <项目根>/logs）
    log_dir: str | None = None
    log_level: str = "INFO"
    log_max_bytes: int = 10_000_000
    log_backup_count: int = 5
    log_to_stdout: bool = True

    # 离线任务参数
    offline_batch_size: int = 50
    offline_lookback_days: int = 7
    offline_max_session_logs: int = 100
    personality_retrieval_top_k: int = 5
    failure_threshold: int = 5

    # 网页正文提取与相关性过滤配置
    web_extract_max_pages: int = 2
    web_extract_max_length: int = 4000
    enable_web_doc_extraction: bool = True

    # 对话记忆保留策略
    memory_dialogue_retention_days: int = 90
    memory_min_recall_count: int = 1
    memory_retrieval_top_k: int = 5
    enable_memory_relevance_filter: bool = False

    # 对话记忆分段（离线写入记忆向量库）
    memory_dialogue_chunk_chars: int = 1200        # 每段字符预算
    memory_dialogue_chunk_overlap_lines: int = 2   # 段间重叠行数（约一轮对话）

    # 是否在 CLI 回复后显示参考来源
    show_references: bool = False

    # DialogueState 中保留的最近对话轮数
    dialogue_state_message_turns: int = 10

    # LLM 上下文 tokenizer 与 token 预算
    tokenizer_model: str = "cl100k_base"
    llm_context_token_limit: int = 30000

    @model_validator(mode="after")
    def _fill_default_paths(self) -> "Settings":
        """填充未显式设置的路径默认值（锚定项目根），填充后字段保持 str。"""
        if self.data_dir is None:
            self.data_dir = str(_PROJECT_ROOT / "data")
        if self.pgembed_data_dir is None:
            self.pgembed_data_dir = str(Path(self.data_dir) / "pgembed")
        if self.chroma_persist_dir is None:
            self.chroma_persist_dir = str(Path(self.data_dir) / "chroma")
        if self.personality_file is None:
            self.personality_file = str(Path(self.data_dir) / "personalities" / "data.json")
        if self.log_dir is None:
            self.log_dir = str(_PROJECT_ROOT / "logs")
        return self


settings = Settings()
