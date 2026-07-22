# 在改进前，首先
* 使用 scripts下的code_struct.py 获取src目录下的代码结构

# 待改进

1. 离线分析时，从sql读出对话内容加入知识向量库，用metadata区分，记录recall 次数，这样可以删除长时间没有用过的对话。

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

# 未来可选增强

- 自动关闭长期未活动的会话（session timeout policy）。
- 可配置的网络搜索提供商（当前仅 DuckDuckGo）。
- 超长会话分片处理，避免超出 LLM 上下文窗口。
- 更完善的日志查看/搜索 UI 或 CLI 命令。
