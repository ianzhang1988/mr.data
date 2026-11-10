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

    # 向量库 embedding 配置（personality / memories 集合共用同一模型；
    # jina v2 无 query/document 前缀要求，文本原样嵌入）
    embedding_model: str = "jinaai/jina-embeddings-v2-base-zh"
    embedding_dim: int = 768
    # 嵌入截断上限（token）。截断发生在 fastembed 内部：load_tokenizer 读取模型
    # 自带 tokenizer_config.json 的 model_max_length（jina-embeddings-v2-base-zh
    # 为 512）并静默截断超长输入，本项目 embeddings.py 无法覆盖它。
    # 若日后换模型或 patch tokenizer_config，须同步修改此配置。
    embedding_max_tokens: int = 512
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
    offline_max_session_logs: int = 100
    # 单会话归因失败重试上限：达到后该会话未处理日志置为已处理（死信），
    # 记 offline.session_dead_lettered，不再无限重试
    offline_max_attribution_attempts: int = 3
    # 单会话归因落库的最大 delta 条数（prompt 收敛 + 代码截断兜底）
    offline_max_deltas_per_session: int = 3
    # 新建性格维度前的 LLM 重合判断（防维度膨胀）；失败时按允许新建降级
    enable_dimension_dedup: bool = True
    personality_retrieval_top_k: int = 5
    # 性格维度淘汰参数：总样本量达到 failure_min_samples 后，
    # 若失败比率 failure_count/(success_count+failure_count) 超过该阈值则停用维度。
    failure_min_samples: int = 10
    failure_ratio_threshold: float = 0.33

    # 网页正文提取与相关性过滤配置
    web_extract_max_pages: int = 2
    web_extract_max_length: int = 4000
    enable_web_doc_extraction: bool = True

    # 记忆保留策略（dialogue 与 web 记忆共用；配置名保留 dialogue 前缀以兼容存量 .env）
    memory_dialogue_retention_days: int = 90
    # 召回免疫门槛：recall_count 达到该次数的记忆不因超期被 prune 删除
    memory_min_recall_count: int = 3
    memory_retrieval_top_k: int = 5
    enable_memory_relevance_filter: bool = False

    # 对话记忆分段（离线写入记忆向量库）
    # 字符预算口径：中文保守估计 1 字 ≈ 1 token，且需容纳段首 overlap 行
    # （约一轮对话），故预算取嵌入截断窗口 embedding_max_tokens 的 ~75%。
    memory_dialogue_chunk_chars: int = 400         # 每段字符预算
    memory_dialogue_chunk_overlap_lines: int = 2   # 段间重叠行数（约一轮对话）

    # web 记忆正文分段（在线管道写入记忆向量库）
    # 与 dialogue 分段同口径（1 字 ≈ 1 token），切口同样句读优先
    memory_web_chunk_chars: int = 400        # web 记忆每片字符预算
    memory_web_chunk_overlap_chars: int = 40 # 片间重叠字符（≤10%）

    # 是否在 CLI 回复后显示参考来源
    show_references: bool = False

    # DialogueState 中保留的最近对话轮数
    dialogue_state_message_turns: int = 10

    # LLM 上下文 tokenizer 与 token 预算
    tokenizer_model: str = "cl100k_base"
    llm_context_token_limit: int = 30000

    @model_validator(mode="after")
    def _check_chunk_budget_within_embedding_limit(self) -> "Settings":
        """分段字符预算不得超过嵌入截断上限。

        按 1 字 ≈ 1 token 保守口径，memory_dialogue_chunk_chars 超过
        embedding_max_tokens 时段尾会被嵌入模型静默截断，直接拒绝启动。
        """
        if self.memory_dialogue_chunk_chars > self.embedding_max_tokens:
            raise ValueError(
                f"memory_dialogue_chunk_chars ({self.memory_dialogue_chunk_chars}) "
                f"不得超过 embedding_max_tokens ({self.embedding_max_tokens})："
                "按 1 字 ≈ 1 token 保守口径，超出的内容会被嵌入模型静默截断。"
            )
        if self.memory_web_chunk_chars > self.embedding_max_tokens:
            raise ValueError(
                f"memory_web_chunk_chars ({self.memory_web_chunk_chars}) "
                f"不得超过 embedding_max_tokens ({self.embedding_max_tokens})："
                "按 1 字 ≈ 1 token 保守口径，超出的内容会被嵌入模型静默截断。"
            )
        return self

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
