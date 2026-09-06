# 在改进前，首先
* 使用 scripts下的code_struct.py 获取src目录下的代码结构

# 加入以下prompt，这是我对你的要求
- 如果还未读取project_object.md 文件，那么读取文件回忆项目目标
- 从待改进部分，去读当前的任务，做必要的分析
  - 首先分析是否合理，有没有重大的脱离当前项目结构的问题，如果有与我交流确认。没有就继续。
  - 计划要做的事情，分成具体的步骤内容。
  - 如果可以并行完成，则使用子agent来加速生产速度。
- 目前项目的概念多了，所以你需要更关注待改进部分范围的修改，对于不在范围中但是你倾向于顺手改的地方，你不要直接改，而是把建议给我，让我来判断。
- 完成修改后，更新本文档

# 待改进

（暂无）

# 已完成的改进项
(保留最近项目，完成项目放到finished_improvement.md中)

35. ✅ **测试共享 PG 状态隔离**：`tests/conftest.py` 新增 function 级 fixture `reset_pg_state`，每个 PG 测试前 `TRUNCATE ... RESTART IDENTITY CASCADE` 8 张业务表并重跑 `seed()`；4 个 PG 测试文件以 `pytestmark = usefixtures` 启用；顺带删除 `test_select_dimensions_uses_core_flag` 的错误还原（dim2 seed 原值是 `core=TRUE`）与 `test_user_identity_crud` 的手动还原。已验证乱序、单文件、连跑两次均无残留。
36. ✅ **删除废弃脚本 `scripts/ingest_personality.py`**：硬编码旧版人格维度/台词，功能被 `mr-data ingest` 完全覆盖，全仓库零引用。
37. ✅ **统一 LLM 结构化输出调用**：合并为统一核心入口 `structured_chat(messages, response_format)`（内置降级+warning 日志+端点能力缓存），`chat_structured` 保留为语法糖，删除 `structured_chat_with_messages`；新增配置 `llm_structured_mode`（auto/parse/prompt）；降级解析新增 `_extract_json`（围栏剥离+首个 JSON 子串提取）；6 个调用点统一迁移，`_think`/web_filter 白得降级能力；新增 `tests/test_llm_structured.py` 10 用例（全量 74 passed, 1 skipped）。
38. ✅ **吞错点统一补 warning 级结构化日志**：按 4 组并行排查（finder/fixer 子 agent 分离），全 src/ 共甄别出 11 处「功能静默退化」吞错点并补齐结构化 warning 日志：graph.py ×4（`personality.dimension_select_failed`/`think.decision_failed`/`retrieve.memory_filter_failed`/`chat.reply_fallback`）、web_filter.py（`web.filter_doc_failed`）、page_extract.py ×2（`web.page_extract_primary_failed`/`web.page_extract_failed`）、prompt_assembly.py（`prompt.compress_failed`）、tokenizer.py（`llm.tokenize_fallback`，warn-once 防刷屏）、chroma.py（`chroma.prune_failed`）、pgembed_manager.py（`db.pgembed_stop_failed`）、cli.py（`cli.eval_score_parse_failed` + rprint 用户提示）；正常控制流（StopIteration、metadata 解析兜底、REPL 退出等）与已有日志点均未动，控制流与兜底语义零改动。补充：`web_search.py`/`search_providers.py` 的裸 `logging.getLogger` 统一为结构化 `get_logger("mr_data.online")`，11+3 处 printf 风格 warning 归并为结构化事件（`web.search_failed`/`web.search_not_configured`/`web.search_provider_unknown`/`web.search_no_results`），至此全项目日志形式统一。

# 未来可选增强(计划中)

- 自动关闭长期未活动的会话（session timeout policy）。
- 更完善的日志查看/搜索 UI 或 CLI 命令。
1. graph.py 中考虑到本地运行使用的模型，例如qwen3.5:9B，目前部分结构化输出对模型的压力可能太大了。我们需要一种兼容性的模式，根据配置来决定是否用复杂的结构化输出方式。（改进项 37 已落地 `llm_structured_mode` 配置与自动降级，剩余：复杂 schema 本身的精简，如块级引用嵌套结构的简化模式）, 我说的其实是model不能完成正确的json输出，甚至连格式都不能保证的情况下。要如何降级的问题。改进37没有处理这类问题的能力。
2. 最后对话组装和生成的部分，是不是考虑做个独立codeagent？给他获取相关内容的能力
3. 做成再cli命令/newsession时，触发 AttributionEngine.run(), 注意不要和按照时间触发的代码发生竞态，也许加个锁，或者其他合适的方式。
1. scripts/init_db.py、scripts/run_offline.py 也是 CLI 命令的薄封装，检查是否有必要
2. 降级路径可加「解析失败重试一次」，本次未加
3. chat_structured 主路径可改用 message.parsed 替代 json.loads，本次为控制 diff 保持现状
