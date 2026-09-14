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
(保留最近项目，完成项目放到finished_improvement.md中；35-50 已迁移归档)
51. ✅ **归因上下文 `_build_context` 重构**：维度按「本次会话激活（新增 `list_session_dimension_ids`，源自 dialogue_dimension_refs）/ 其余活跃（对照去重）」分组；删除向量库人格素材检索段；内心独白改从 PG `metadata.inner_monologue` 并入 transcript 逐行展示，`read_session_events` think 日志段落删除；system prompt 同步；新增 `tests/test_attribution_context.py` 4 用例（全量 122 passed, 1 skipped）。
52. ✅ **删除 `log_dir` 死参数与 `read_session_events`**：`AttributionEngine.__init__` 去掉 `log_dir` 参数（生产构造点 cli.py 本就没传）；`logging.py` 删除无调用方的 `read_session_events` 及随之失效的 `typing.Any/Optional` import；测试 8 处调用点去实参、两个 `_make_engine` helper 去死参数（`temp_log_dir` fixture 保留，仍负责 settings.log_dir 重定向）。全量 122 passed, 1 skipped 不变。
53. ✅ **归因 prompt 增强（新维度字段澄清 + LLM 去重判断 + 收敛 3 条强关联）**：`DimensionDelta.description` 改名 `new_dimension_description` 并新增 `new_dimension_reason`（新建维度必填理由，仅进 prompt 与落库 info 日志，不动 PG schema）；system prompt 明确新建须逐条对照既有维度、拿不准归入既有、最多 3 条按关联强度降序；新增 `DimensionDedupMatch`/`DimensionDedupResult`（models/personality.py）与 `_dedup_new_dimensions`（事务外批量一次 LLM 调用，`temperature=0.0`，命中则改挂既有维度，幻觉 id 忽略，失败降级为允许新建），去重后按 `offline_max_deltas_per_session`（默认 3）截断；新增配置 `enable_dimension_dedup`（默认 True）与 `offline_max_deltas_per_session`；FakeLLM 加 `DimensionDedupResult` 分支；新增 `tests/test_attribution_dedup.py` 6 用例（顺带补齐 `insert_dimension` 新维度路径零覆盖）。全量 128 passed, 1 skipped（基线 122 → +6）。
54. ✅ **LLM 幻觉 id 防护（transcript 带日志 id + 返回 id 校验 + 移除 fallback 污染）**：`_build_transcript` 拼行带日志 id（`assistant[#123]（内心独白：…）: 内容`），system prompt 第 6 条要求 target 必须取自 assistant 行 [#编号]；新增 `_validate_delta_ids`（事务外、去重截断后调用）——`dimension_id` 不在活跃白名单置 None（幻觉 id 不再触发 adjustment_logs 外键违规炸批处理）、`target_dialogue_log_id` 不在本会话 assistant 行集合内置 None（防幻觉/防 user 行/防跨 session 串号），均记 warning；`_apply` 删除 `fallback_assistant_id` 兜底，无有效 target 时 adjustment 仍落库（dialogue_log_id=NULL）但 evidence/event 的 Chroma 写入整体跳过（不再产生 "None" 哈希孤儿文档，记 info 日志）；`_build_evidence_context` None 分支删除；FakeLLM 新增可设置属性 `attribution_target_log_id`，test_e2e/test_attribution_transaction 两处依赖 fallback 的用例显式指定 target；新增 `tests/test_attribution_id_guard.py` 6 用例。全量 134 passed, 1 skipped（基线 128 → +6）。
55. ✅ **离线批处理会话级容错 + 在线维度选择幻觉 id 日志**：`run()` 的 `_apply` 事务段包 try/except——单会话崩溃回滚（transaction() 自动 rollback）留待重跑、记 warning（`offline.apply_failed`）并继续后续会话，`offline.completed` details 新增 `failed_sessions`；`run()` 不再传播单会话 apply 异常（两个既有 transaction 测试去 `pytest.raises` 改新语义），新增批处理容错用例（A 崩 B 正常、A 恢复重跑只应用一次）。在线侧经核实 `_select_dimensions` 白名单过滤**已存在**（graph.py:183-186），仅补 dropped ids warning（`personality.dimension_select_invalid_ids`）与幻觉 id 过滤测试。全量 136 passed, 1 skipped（基线 134 → +2）。
56. ✅ **用户打分闭环（退出时会话级评分 → 离线归因参考）**：sessions 表新增 `rating`（-2..2）/ `rating_comment` 列与 `Session` 模型字段，`create_session` 同 id 复用时重置评分；新增 `update_session_rating`；cli.py 新增 `_ask_session_rating`（`/exit`/`/newsession`/Ctrl+C 退出时问一次评分+可选评论，空输入跳过、非法输入记 warning 跳过），移除 `--eval` 逐轮评分与无调用方的 `update_evaluation`；归因 `_build_context` 追加「用户对本会话的评价」段（无评分省略），system prompt 新增「用户评分是最直接成败信号」条款；新增 `tests/test_session_rating.py` 6 用例（全量 142 passed, 1 skipped）。

# 未来可选增强(计划中)

- 自动关闭长期未活动的会话（session timeout policy）。
- 更完善的日志查看/搜索 UI 或 CLI 命令。
1. graph.py 中考虑到本地运行使用的模型，例如qwen3.5:9B，目前部分结构化输出对模型的压力可能太大了。我们需要一种兼容性的模式，根据配置来决定是否用复杂的结构化输出方式。（改进项 37 已落地 `llm_structured_mode` 配置与自动降级，剩余：复杂 schema 本身的精简，如块级引用嵌套结构的简化模式）, 我说的其实是model不能完成正确的json输出，甚至连格式都不能保证的情况下。要如何降级的问题。改进37没有处理这类问题的能力。
2. 最后对话组装和生成的部分，是不是考虑做个独立codeagent？让他有更好的思考完成任务的机会，可以考虑再加上获取相关内容的能力
3. 做成再cli命令/newsession时，触发 AttributionEngine.run(), 注意不要和按照时间触发的代码发生竞态，也许加个锁，或者其他合适的方式。
2. 降级路径可加「解析失败重试一次」，本次未加
3. chat_structured 主路径可改用 message.pagsed 替代 json.loads，本次为控制 diff 保持现状
4. metadata schema 化残留（改进 49 范围外）：web doc 管道流转裸 dict（search_providers/web_filter/graph）与 DialogueState 的 list[dict] 粒度；chunk_dialogue_logs 内部 chunk dict key 名（first_log_id）与落库 metadata（first_dialogue_log_id）不一致；collection 级配置 metadata 无类；increment_memory_recall 就地 dict 修改未走模型
5. 检查现在配置和openapi调用，是否兼容deepseek

4. chroma性格库部分，增加召回时的记录，用来做淘汰等
3. 读一下 review/group_reviews/G10_文档与配置.md 这里提到多处文档没有更新，发起子agent确认问题，属实的话发起子agent修复，你作为管理者，核验修复结果。
