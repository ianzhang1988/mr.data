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
38. ✅ **吞错点统一补 warning 级结构化日志**：（略，详见 finished_improvement.md）
39. ✅ **统一 source_type 声明与实现**：（略，详见 finished_improvement.md）
40. ✅ **测试 Chroma 改内存模式提速**：（略，详见 finished_improvement.md）
41. ✅ **测试 pgembed 集群跨运行复用**：`pgembed_server` fixture 从每次临时目录 initdb（11-15s/次）改为固定目录 `/tmp/mr-data-pgembed-test-<uid>` 复用；兜底：`SCHEMA_SQL` sha256 指纹标记文件，不一致删目录重建；启动失败（脏锁/损坏）删目录重试一次。数据隔离由既有 `reset_pg_state` 每用例 TRUNCATE+seed 保证。仅动 `tests/conftest.py`，业务代码零改动。实测冷启动 61.5s、复用 46.5s/39.7s、指纹篡改后自动重建 53.1s，四轮均 78 passed。

# 未来可选增强(计划中)

- 自动关闭长期未活动的会话（session timeout policy）。
- 更完善的日志查看/搜索 UI 或 CLI 命令。
1. graph.py 中考虑到本地运行使用的模型，例如qwen3.5:9B，目前部分结构化输出对模型的压力可能太大了。我们需要一种兼容性的模式，根据配置来决定是否用复杂的结构化输出方式。（改进项 37 已落地 `llm_structured_mode` 配置与自动降级，剩余：复杂 schema 本身的精简，如块级引用嵌套结构的简化模式）, 我说的其实是model不能完成正确的json输出，甚至连格式都不能保证的情况下。要如何降级的问题。改进37没有处理这类问题的能力。
2. 最后对话组装和生成的部分，是不是考虑做个独立codeagent？让他有更好的思考完成任务的机会，可以考虑再加上获取相关内容的能力
3. 做成再cli命令/newsession时，触发 AttributionEngine.run(), 注意不要和按照时间触发的代码发生竞态，也许加个锁，或者其他合适的方式。
1. scripts/init_db.py、scripts/run_offline.py 也是 CLI 命令的薄封装，检查是否有必要
2. 降级路径可加「解析失败重试一次」，本次未加
3. chat_structured 主路径可改用 message.parsed 替代 json.loads，本次为控制 diff 保持现状
1.  这个项目里，三条写入路径的幂等性完全不一样：

  ┌────────────────────────────────────────────────────────────────────────────────────────────┬─────────────────────────────────────────────────────────────────────────────┬──────────────────┐
  │ 写入路径                                                                                   │ id 生成方式                                                                 │ 幂等？           │
  ├────────────────────────────────────────────────────────────────────────────────────────────┼─────────────────────────────────────────────────────────────────────────────┼──────────────────┤
  │ PG 各表                                                                                    │ UNIQUE 约束 + ON CONFLICT                                                   │ ✅               │
  │ Chroma memories 的 web 资料                                                                │ URL 的 sha256（graph.py:687 upsert_memory，id 来自 search_providers.py:19） │ ✅（有洞，见下） │
  │ Chroma personality 全部写入（add_personality_event，chroma.py:101）                        │ 每次 uuid.uuid4()                                                           │ ❌               │
  │ Chroma memories 的对话分块（add_memory，chroma.py:169；attribution.py:350 不传 memory_id） │ 每次 uuid.uuid4()                                                           │ ❌               │
  └────────────────────────────────────────────────────────────────────────────────────────────┴─────────────────────────────────────────────────────────────────────────────┴──────────────────┘

  Chroma 的 add 以 id 为去重键——id 每次都是新随机数，等于去重机制形同虚设。

web 资料用 URL sha256 做 id，是对的。但 _stable_web_id（search_providers.py:16-22）：

  if url:   return sha256(url)
  if body:  return "web:" + sha256(body)[:16]
  return f"web:{uuid.uuid4().hex}"      # ← 洞 1：无 URL 无正文时退化回随机 id

  • 洞 1：搜索结果既无 URL 又无正文时退化 uuid4，同一结果重复出现就重复入库；
  • 洞 2：百度/360 的跳转链接未还原成真实 URL——同一个页面，今天经跳转链接 A 搜到、明天经跳转链接 B 搜到，sha256 不同 → 同一页面在记忆库重复。表现：记忆库缓慢膨胀，"世界知识"检索返回同一页面的多个副本。
  例 5（系统层面）：重试语义不对称，崩溃恢复无法安全重放
2. config.py 全部默认路径（`.env`、`./data/...`、`./logs`）都是工作目录，默认用“main”脚本所在目录，增加数据目录的配置项目。保证在运行阶段目录不会飘移。
3. 读一下 review/group_reviews/G10_文档与配置.md 这里提到多处文档没有更新，发起子agent确认问题，属实的话发起子agent修复，你作为管理者，核验修复结果。
4. chroma_recreate_on_mismatch=True 会造成数据丢失的问题，改为发现问题把数据导出，并打印这个行为，完成时给个报告。增加一个在新的chroma回复数据的功能，放到命令行中，这样数据不会丢失，是否使用老数据的判断留给用户。
5.  ┌───┬────────────────────────┬────────────────────────────┬────────┬─────────────────────┐
  │ # │ 数据                   │ PG                         │ Chroma │ Chroma 删了能找回吗 │
  ├───┼────────────────────────┼────────────────────────────┼────────┼─────────────────────┤
  │ 1 │ 对话原文（用户+助手）  │ ✅ dialogue_logs           │ —      │ —                   │
  │ 2 │ 内心独白、回复引用     │ ✅ dialogue_logs.metadata  │ —      │ —                   │
  │ 3 │ 权重调整（+0.1）及审计 │ ✅ adjustment_logs         │ —      │ —                   │
  │ 4 │ 证据片段 evidence      │ ✅ vector_refs.content     │ ✅     │ ✅ 从 PG 重建       │
  │ 5 │ 会话分块记忆           │ ✅（原文在 dialogue_logs） │ ✅     │ ✅ 重新分块         │
  │ 6 │ 事件总结 event_summary │ ❌ 无任何记录              │ ✅     │ ❌ 永久丢失         │
  └───┴────────────────────────┴────────────────────────────┴────────┴─────────────────────┘
在离线处理时，event_summary没有加到pg里面。这里具体的逻辑还没有看。不过这个似乎应该加到pg作为一个新的维度。这部分需要先看代码，再说。
