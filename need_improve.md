# 待改进

1. personality_dimensions中增加一个列，标记是否是固定核心性格。需要这种固核心定性格来保持角色的稳定性。
2. src/mr_data/online/graph.py 中
  - 在_think前增加一步，根据当前用户input，让llm选出应当起作用的多个核心性格（personality_dimensions）
  - _think时，考虑在prompt中，增加上一步选出的核心性格作为查询的参考。
  - retrieve_web extract_web_pages 应该是可选的分支，增加conditional_edges
  - extract_web_pages 后应该用llm挑选出和用户输入相关的文档，并更新web_docs, 例如可以对每个文档调用llm，结合用户input，让llm判断是否保留
  - _log_dialogue 写入记忆向量库部分，增加web_docs内容，作为新的世界知识，增加时间等metadata，以便后续做其他功能

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

# 未来可选增强

- 自动关闭长期未活动的会话（session timeout policy）。
- 可配置的网络搜索提供商（当前仅 DuckDuckGo）。
- 超长会话分片处理，避免超出 LLM 上下文窗口。
- 更完善的日志查看/搜索 UI 或 CLI 命令。
