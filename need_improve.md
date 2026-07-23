# 在改进前，首先
* 使用 scripts下的code_struct.py 获取src目录下的代码结构

# 待改进

1. graph.py 中 llm.chat 相关的调用，统一使用结构化输出的形式。（考虑这个问题是，判断一下是否对适配不同的llm有帮助）
2. 在 DialogueState 里加 messages, 保留最近 5~10 轮对话。考虑下保留啥，插入记忆库的就不需要了。
3. 在web_search.py中增加更多搜索工具，可以增加搜索引擎的爬虫代码，如bing，google，百度，360等等。可配置是否使用，并按照配置的顺序作为优先级，搜索失败就退一级搜索。
4. graph.py 中，在_retrieve_memories后增加一个通过配置可选的节点，用llm去筛选记忆库返回的内容中，与用户输入相关的内容。这让当扩大记忆库的top_k后，可以用llm来精简内容，提升后面回答的质量

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

# 未来可选增强

- 自动关闭长期未活动的会话（session timeout policy）。
- 超长会话分片处理，避免超出 LLM 上下文窗口。
- 更完善的日志查看/搜索 UI 或 CLI 命令。
