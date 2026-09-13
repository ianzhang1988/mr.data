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

（空）

# 已完成的改进项
(保留最近项目，完成项目放到finished_improvement.md中；35-44 已迁移归档)

45. ✅ **在线 `_log_dialogue` 事务化**：4 步 PG 写包入 `pg.transaction()`，中途失败整体回滚不留脏数据；Chroma upsert 留事务外；新增 `tests/test_log_dialogue_transaction.py` 2 用例。
46. ✅ **config 路径锚定项目根 + 数据目录配置项**：默认路径不再相对 CWD；新增 `MR_DATA_DATA_DIR`；新增 `tests/test_config_paths.py` 5 用例。
47. ✅ **Chroma 重建防丢数据（导出+报告+恢复 CLI）**：mismatch 删除前先导出 JSON 备份（导出失败阻止删除）、重建后打印报告；新增 `mr-data chroma-restore`；新增 `tests/test_chroma_backup.py` 4 用例（全量 106 passed, 1 skipped）。
48. ✅ **Prompt 预算分配 priority 梯度生效（贪心注水）**：`_compress_to_budget` 超预算分支改为按 priority 降序分层注水——高优先级组整组原文保留，首个装不下的组按体积分摊剩余，预算耗尽的组输出省略占位符；must_keep 极端超预算分支顺带变为 100→90→80 降序注水；新增 `tests/test_prompt_assembly.py` 5 用例（全量 111 passed, 1 skipped）。
49. ✅ **落库 metadata 全面 schema 类化**：新增 `DialogueLogMetadata`（PG jsonb）、`PersonalityDocMetadata`（personality 集合，CSV↔list[int] 转换集中）、`DialogueMemoryMetadata`/`WebMemoryMetadata`（memories 集合两套形态）4 个 schema 类；`add_memory`/`upsert_memory` 参数收紧为模型 Union；新增 `tests/test_metadata_schema.py`，陈旧测试形态对齐（全量 118 passed, 1 skipped）。
50. ✅ **event_summary 写入 PG adjustment_logs**：`adjustment_logs` 加列 `event_summary TEXT`（幂等迁移），`AdjustmentLog`/`insert_adjustment`/`_apply` 贯通，与 delta 一一对应；新增 `tests/test_event_summary_pg.py` 2 用例。

# 未来可选增强(计划中)

- 自动关闭长期未活动的会话（session timeout policy）。
- 更完善的日志查看/搜索 UI 或 CLI 命令。
1. graph.py 中考虑到本地运行使用的模型，例如qwen3.5:9B，目前部分结构化输出对模型的压力可能太大了。我们需要一种兼容性的模式，根据配置来决定是否用复杂的结构化输出方式。（改进项 37 已落地 `llm_structured_mode` 配置与自动降级，剩余：复杂 schema 本身的精简，如块级引用嵌套结构的简化模式）, 我说的其实是model不能完成正确的json输出，甚至连格式都不能保证的情况下。要如何降级的问题。改进37没有处理这类问题的能力。
2. 最后对话组装和生成的部分，是不是考虑做个独立codeagent？让他有更好的思考完成任务的机会，可以考虑再加上获取相关内容的能力
3. 做成再cli命令/newsession时，触发 AttributionEngine.run(), 注意不要和按照时间触发的代码发生竞态，也许加个锁，或者其他合适的方式。
2. 降级路径可加「解析失败重试一次」，本次未加
3. chat_structured 主路径可改用 message.parsed 替代 json.loads，本次为控制 diff 保持现状
4. metadata schema 化残留（改进 49 范围外）：web doc 管道流转裸 dict（search_providers/web_filter/graph）与 DialogueState 的 list[dict] 粒度；chunk_dialogue_logs 内部 chunk dict key 名（first_log_id）与落库 metadata（first_dialogue_log_id）不一致；collection 级配置 metadata 无类；increment_memory_recall 就地 dict 修改未走模型

1.
attribution.py 中 _build_context, 注意下面代码 ## 后的问题
```
        return f"""{identity_text}

当前活跃的性格维度：
{dim_text}  ## 应该分成两组，当前会话激活的，和剩余的，作为推到新性格时去重的工具

与本次会话相关的人格素材（来自向量库）：
{personality_text if personality_text else '（暂无）'} ## 没有必要放在这里。
助手在本次会话中的思考过程（检索查询、内心独白等）： ## 只有内心独白部分有实际作用，而且应该和对话放在一起，而不是这里
{thought_text if thought_text else '（暂无）'}
""".strip()
```
2. attribution.py 中,
  1. 这两条用于发现新的性格维度的prompt是不是太单薄了，description这个名字也不清晰，而system中处理描述新的性格外，应该明确的让llm把新增的理由填写到对应字段。
  另外，还有个严重的问题，这种方式，可能和原来接近的性格，考虑再增加一次llm调用判断新增的是否和原来的重合，直接加到数据库，有膨胀的风险。
    - description: Optional[str] = Field(default=None, description="新建维度时的描述；与 dimension_id 二选一")
    - system prompt中 如果维度已存在，给出其 dimension_id（整数）；如果是新维度，给出 description（描述性自白）。
  2. attribute_session 中的system prompt，应该要求把推导出的性格收敛为3个。我希望的是筛选较强的关联，不要牵强的关系。新的维度生成也是，应该有明显的新的性格表现时。
2. attribution.py
  _build_transcript 拼行时带上日志 id（如 assistant[#123]: {content}），让 LLM 有据可依；对应后面的target_dialogue_log_id。同时检查一下，是否还有其他遗漏
  从llm返回的所有数据库相关id要先验证,要考虑到llm容易幻觉id这类文本的摘取
  target_log_id = delta.target_dialogue_log_id or fallback_assistant_id 这个兜底逻辑可能造成数据污染，尤其是对证据入库那部分，依赖这里生成查询key，不应该用fallback，应该跳过。
  ① LLM 给的 id 不做校验，且能炸掉整个事务。 LLM 输出的 target_dialogue_log_id 没有验证"是否属于本 session、是否真实存在"。而 adjustment_logs.dialogue_log_id 有外键约束——LLM 幻觉一个不存在的 id 时，INSERT 触发 FK 违规，且 run()
  里 _apply 的事务段（attribution.py:124-127）没有 try/except，异常会直接冒泡中断整个离线批处理。

  ② None 的污染路径。 若 LLM 没给 id 且 session 里恰好没有 assistant 日志，target_log_id = None：
  • adjustment_logs.dialogue_log_id 写入 NULL（列允许）；
  • Chroma 的 evidence/event 文档照样写入，但 doc id 的哈希原料变成字符串 "None"，source_id 也是 "None"；
  • 且 dialogue_vector_refs 因 if target_log_id is not None（attribution.py:294）跳过不写——产生"Chroma 有证据、PG 无反向引用"的孤儿文档（之前 review 文件里记的"内部问题 3：source_id 'None' 污染"就是这个）。
  ③ 跨 session 串号无防护。 LLM 完全可能输出属于别的会话的 log id——代码不检查归属，于是 adjustment/vector_refs 会把 A 会话的归因挂到 B 会话的对话上；_build_evidence_context 虽然会因找不到而兜底，但 PG 里的关联已经错了。
2. 用户打分部分
  采集端src/mr_data/cli.py：退出会话时问一次, 而不是每轮对话问一次, 评分，评论可选
  使用端：在离线处理时，llm对性格维度评分时，参考会话的打分，还有评论（如果有，可能包含对某些性格模式的批评，就需要对改性格减分。也可能反过来）
  存储端：看看pg是否需要适配上面的调整
4. chroma性格库部分，增加召回时的记录，用来做淘汰等
3. 读一下 review/group_reviews/G10_文档与配置.md 这里提到多处文档没有更新，发起子agent确认问题，属实的话发起子agent修复，你作为管理者，核验修复结果。
