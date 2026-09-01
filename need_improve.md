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

29. ✅ **`_filter_web_docs` 逐文档判断与段落摘取**：新增 `WebDocExtraction` 模型与 `online/web_filter.py`；每条 web 文档单独 LLM 调用，先判断相关性，相关则摘取/简化相关内容替换 `page_content`，不相关丢弃；单文档异常保留原文档，全部丢弃时 fallback 保留全部；新增 `enable_web_doc_extraction` 配置（默认开启）；移除旧的批量过滤模型 `WebRelevanceItem`/`WebRelevanceFilterResult`。
30. ✅ **graph.py 帮助函数拆出**：新增 `online/prompt_assembly.py`，移入 `PromptSection`、`wrap_section`、`build_messages` 与 `PromptAssembler` 类（`fit_sections`/`_compress_to_budget`/`_compress_text`）；graph.py 保留 `DialogueState`/`ChatResult`/reducer 与节点组装逻辑。
31. ✅ **对话记忆改为离线按会话分段写入**：删除 `_log_dialogue` 中每轮无 metadata 的逐条 `add_memory`（野数据，无 `source_type` 标签、不参与 recall 计数与清理）；`attribution.py` 新增 `chunk_dialogue_logs`（按字符预算分段、段间保留 overlap 行）与 `_persist_session_memories`，离线归因后按会话整体分段写入记忆向量库，metadata 含 `source_type="dialogue"`、`chunk_index`、首尾 `dialogue_log_id`、`recall_count` 等完整标签；新增配置 `memory_dialogue_chunk_chars`（默认 1200）与 `memory_dialogue_chunk_overlap_lines`（默认 2）。
32. ✅ **删除 DialogueState reducer（`_merge_docs`/`_merge_messages`）**：reducer 并集/拼接语义与节点 `{**state, ...}` 返回方式冲突，导致 web 文档过滤/提取结果被还原、messages 每节点翻倍；图为线性无并行分支，reducer 无必要，4 个字段改为普通 overwrite 语义。
33. ✅ **归因失败不再误标记已处理**：`_attribute_session` 异常时返回 `None`（而非空结果），`run()` 检测到失败后 `continue`，不写对话记忆、不 `mark_dialogue_processed`，留待下次离线运行重试。
34. ✅ **选中维度在 assemble 与 log 中生效**：`_assemble_and_generate` 的 `dim_text` 与 `_log_dialogue` 的 `insert_dialogue_dimension_refs` 改用 `selected_dimension_ids` 过滤（空时回退全量），不再使用全量维度。

# 未来可选增强(计划中)



- 自动关闭长期未活动的会话（session timeout policy）。
- 更完善的日志查看/搜索 UI 或 CLI 命令。
1. graph.py 中考虑到本地运行使用的模型，例如qwen3.5:9B，目前部分结构化输出对模型的压力可能太大了。我们需要一种兼容性的模式，根据配置来决定是否用复杂的结构化输出方式。
2. 最后对话组装和生成的部分，是不是考虑做个独立codeagent？给他获取相关内容的能力
3. 做成再cli命令/newsession时，触发 AttributionEngine.run(), 注意不要和按照时间触发的代码发生竞态，也许加个锁，或者其他合适的方式。
