
# 已完成的改进项

1. ✅ 增加 `sessions` 表；CLI 支持 `/newsession` 切换会话；`dialogue_logs` 与 `adjustment_logs` 增加 `session_id`；离线归因按已关闭会话处理。
2. ✅ LangGraph 在线流水线增加 Web Search RAG 节点（默认开启，基于 DuckDuckGo）。
3. ✅ 引入 `pgembed` 作为默认数据库（测试使用临时目录，日常使用持久化目录），并基于它完成功能测试。
4. ✅ 离线归因改为**会话级 transcript**：按时间顺序拼接 `user` / `assistant` 对话，避免顺序混乱。
5. ✅ 离线归因提示词注入**基础人设、当前性格维度、历史人格向量库素材**作为上下文。
6. ✅ 归因时同步提取**关键证据片段**写入性格向量库（`source_type="evidence"`），并标记与基础性格的关系（`relation_to_personality`）；同时在 `dialogue_vector_refs` 记录反向引用。
7. ✅ 维度失败次数达到 `MR_DATA_FAILURE_THRESHOLD` 时自动**标记失效**，并清理 `personality` 向量库中对应的证据文档与 Postgres 引用记录。
8. ✅ 性格向量库改为**场景上下文 embedding + agent 台词 utterance**：`PersonalityEvent` 新增 `context` 与 `speaker`；Chroma 存储时嵌入完整场景，检索返回时只取 `metadata.utterance`。
9. ✅ **结构化日志系统**：新增 `src/mr_data/logging.py`，JSONL 输出到 `./logs/mr-data.log`，支持滚动；在线对话记录检索查询与内心独白，离线归因读取思考过程并纳入提示词。
10. ✅ **网页正文提取工具**：新增 `PageExtractor`（`trafilatura` + `requests/BeautifulSoup` fallback），接入 LangGraph，在 web search 后提取页面正文。
11. ✅ **固定核心性格标记**：`personality_dimensions` 增加 `core` 列；核心维度不会被离线归因自动失效，保持角色稳定性。
12. ✅ **在线核心性格选择**：`DialogueGraph` 在 `_think` 前增加 `_select_dimensions` 节点，由 LLM 根据用户输入选出最应起作用的性格维度，并注入 `_think` 提示词。
13. ✅ **Web 检索条件分支**：`retrieve_web`、`extract_web_pages` 改为 `conditional_edges`，根据配置和中间结果动态跳过。
14. ✅ **网页资料 LLM 相关性过滤**：`extract_web_pages` 后可选通过 LLM 逐文档判断与用户输入的相关性，保留相关文档。
15. ✅ **网络资料写入世界知识记忆**：`_log_dialogue` 把 `web_docs` 写入 `memories` 向量库，附带 `source_type=web`、URL、标题、检索时间、查询等 metadata。
16. ✅ **默认人格改为 Data**：`PostgresStore.seed()` 默认人格原型改为《星际迷航：下一代》中的 Data；新增 `PersonalityPack`/`PersonalitySampleLine` 模型与 `personality_loader`，支持从 `data/personalities/*.json` 加载人格，代码常量作为兜底；`mr-data ingest` 从人格包读取示例台词。
17. ✅ **Chroma 高级 Embedding**：`personality` 集合改用 `fastembed` + `nomic-ai/nomic-embed-text-v1.5` 并截断至 512 维，`memories` 集合改用 `BAAI/bge-base-zh-v1.5` 768 维；代码中自动添加 Nomic/BGE 所需的 query/document 前缀；旧集合维度不一致时自动重建。
18. ✅ **用户身份设定**：新增 `user_identities` 表，支持保存多个用户身份；seed 时写入 Picard（默认、受保护）与普通用户（受保护）；`DialogueGraph._assemble_and_generate` 从数据库读取当前默认身份并注入 system prompt；CLI 新增 `mr-data identity list/add/edit/delete/select` 管理身份。
19. ✅ **交互式帮助命令**：`mr-data chat` 中输入 `/help` 或 `/?` 可显示当前 slash 命令、启动选项及顶层 CLI 命令。
20. ✅ **think 节点结构化决策**：`DialogueGraph._think` 使用 `ThinkDecision` 结构化输出，生成 `personality_query`、`memory_query`、`needs_web_search`、`search_query` 与 `inner_monologue`；web 分支仅由 think 决策和 `enable_web_search` 单一开关控制，`retrieve_web` / `extract_web_pages` / `filter_web_docs` 作为整体流水线依次执行。
21. ✅ **离线对话记忆与 recall 计数**：离线归因后将对话日志写入 `memories` 向量库（`source_type=dialogue`），记录 `recall_count`；在线检索命中对话记忆时递增计数；新增 `prune_stale_dialogue_memories` 清理长期未召回的旧对话记忆。
22. ✅ **统一结构化输出与 LLM 适配降级**：`LLMClient` 新增 `structured_chat`，先尝试 OpenAI `parse` API，失败时自动降级为普通 chat + JSON Schema prompt + 解析；`_select_dimensions`、`_filter_web_docs`、离线归因统一改为结构化输出，提升不同 LLM 端点的适配性。
23. ✅ **多源 Web 搜索与失败降级**：新增 `SearchProvider` 协议与 `search_providers.py`，支持 DuckDuckGo、SearXNG、Brave、Bing、Google CSE、百度、360；`WebSearchTool` 改为按配置顺序调用，失败自动降级；新增 `web_search_providers` 等配置项。
24. ✅ **记忆相关性过滤节点**：`_retrieve_memories` 后增加可选 `_filter_memory_docs` 节点，由 LLM 批量判断记忆与用户输入的相关性，扩大 `memory_retrieval_top_k` 后自动精简记忆内容；新增 `enable_memory_relevance_filter` 配置。
25. ✅ **`_assemble_and_generate` 结构化输出与参考来源**：`AssistantReply`/`ReplyReference` 模型定义回复文本与参考列表；生成时要求 LLM 输出每条参考的 id、source_type 和一句话 summary；CLI 新增 `--show-references`/`show_references` 配置控制是否显示参考来源。
26. ✅ **Web 搜索结果长期稳定 id**：`_to_doc_format` 改用 URL SHA256 哈希作为 doc id；新增 `ChromaStore.upsert_memory`，`_log_dialogue` 写入网络资料时使用稳定 id 并作为全局知识（`session_id=""`），避免 `web:0/web:1` 每轮重置导致的冲突。
27. ✅ **DialogueState 保留最近 N 轮 messages**：`dialogue_logs` 新增 `metadata` JSONB 字段，用于保存 assistant 回复的 `inner_monologue` 和 `reply_references`；每轮启动时从 Postgres 加载最近对话构造 `DialogueMessage` 列表；`DialogueState` 新增 `messages` 字段与 reducer；配置项 `dialogue_state_message_turns` 默认 10。
28. ✅ **`_assemble_and_generate` Token 预算与提示词结构**：引入 `tiktoken` 实现 `TokenCounter`；新增 `llm_context_token_limit`（默认 30K）与 `tokenizer_model` 配置；按优先级组织 prompt section：system / identity+核心性格 / 用户输入 / personality / memory / messages / web / format；超限时按优先级从低到高压缩，低优先级内容调用 LLM 摘要或硬性截断。进一步将上下文材料从 system prompt 拆分到 `assistant` role 消息中，system 首段明确指引 LLM 如何使用人格/记忆/对话/web 素材；assistant 消息中每类素材使用 XML 标签（`<personality>`/`<memory>`/`<messages>`/`<web>`）包裹，便于模型识别分类。
29. ✅ **`_filter_web_docs` 逐文档判断与段落摘取**：新增 `WebDocExtraction` 模型与 `online/web_filter.py`；每条 web 文档单独 LLM 调用，先判断相关性，相关则摘取/简化与用户输入相关的内容替换 `page_content`（metadata 标记 `llm_extracted`），不相关丢弃；单文档异常保留原文档，全部丢弃时 fallback 保留全部；新增 `enable_web_doc_extraction` 配置（默认开启）；移除旧的批量过滤模型 `WebRelevanceItem`/`WebRelevanceFilterResult`。
30. ✅ **graph.py 帮助函数拆出**：新增 `online/prompt_assembly.py`，移入 `PromptSection`、`wrap_section`、`build_messages` 与 `PromptAssembler` 类（`fit_sections`/`_compress_to_budget`/`_compress_text`）；graph.py 保留 `DialogueState`/`ChatResult`/reducer 与节点组装逻辑。
31. ✅ **对话记忆改为离线按会话分段写入**：删除 `_log_dialogue` 中每轮无 metadata 的逐条 `add_memory`（野数据，无 `source_type` 标签、不参与 recall 计数与清理）；`attribution.py` 新增 `chunk_dialogue_logs`（按字符预算分段、段间保留 overlap 行）与 `_persist_session_memories`，离线归因后按会话整体分段写入记忆向量库，metadata 含 `source_type="dialogue"`、`chunk_index`、首尾 `dialogue_log_id`、`recall_count` 等完整标签；新增配置 `memory_dialogue_chunk_chars`（默认 1200）与 `memory_dialogue_chunk_overlap_lines`（默认 2）。
32. ✅ **删除 DialogueState reducer（`_merge_docs`/`_merge_messages`）**：reducer 并集/拼接语义与节点 `{**state, ...}` 返回方式冲突，导致 web 文档过滤/提取结果被还原、messages 每节点翻倍；图为线性无并行分支，reducer 无必要，4 个字段改为普通 overwrite 语义。
33. ✅ **归因失败不再误标记已处理**：`_attribute_session` 异常时返回 `None`（而非空结果），`run()` 检测到失败后 `continue`，不写对话记忆、不 `mark_dialogue_processed`，留待下次离线运行重试。
34. ✅ **选中维度在 assemble 与 log 中生效**：`_assemble_and_generate` 的 `dim_text` 与 `_log_dialogue` 的 `insert_dialogue_dimension_refs` 改用 `selected_dimension_ids` 过滤（空时回退全量），不再使用全量维度。
