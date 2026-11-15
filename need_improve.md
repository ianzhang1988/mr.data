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

（当前无进行中改进项；下一截断调查项目见 review/truncation-survey.md「改进方案」D，待指示）

# 已完成的改进项
（保留最近三条，完成的条目放到 finished_improvement.md 中归档；35-80 已迁移归档）

81. ✅ chat_structured 主路径改用 `message.parsed`：`_structured_chat_via_parse` 优先消费 SDK 解析好的 `message.parsed`（不再对 content 二次 json.loads），parsed=None 保留 content 兜底。详见 finished_improvement.md #81。

82. ✅ 离线归因拆批语义（截断调查改进项 B1-B4）：单会话超 100 条未处理日志跨 run 拆批——批间上文回顾（adjacent-processed 查询 + 不可作 target）、分片级死信（不再会话级连坐）、「第 x/y 批」标注、评分段落拆批时替换为背景校准+锚定本批事件。未拆批 prompt 零变化。详见 finished_improvement.md #82。

83. ✅ 网页管道截断改造（截断调查改进项 C1-C3）：抽取归档上限 4000→100000 带省略标记；filter prompt 新增 `web_filter_max_tokens=32768` 总预算、超额纯头部截 LLM 副本（源数据不动）记 `web.filter_prompt_truncated`；入库双轨——`WebDoc.original_content` 原文切片 + 哈希基准改原文，修同页重复分片。详见 finished_improvement.md #83。

# 未来可选增强（计划中）

2. 更完善的日志查看/搜索 UI 或 CLI 命令。
3. graph.py 中考虑到本地运行使用的模型，例如qwen3.5:9B，目前部分结构化输出对模型的压力可能太大了。我们需要一种兼容性的模式，根据配置来决定是否用复杂的结构化输出方式。（改进项 37 已落地 `llm_structured_mode` 配置与自动降级，剩余：复杂 schema 本身的精简，如块级引用嵌套结构的简化模式）, 我说的其实是model不能完成正确的json输出，甚至连格式都不能保证的情况下。要如何降级的问题。改进37没有处理这类问题的能力。
4. 最后对话组装和生成的部分，是不是考虑做个独立codeagent？让他有更好的思考完成任务的机会，可以考虑再加上获取相关内容的能力
8. 考虑性格向量库的更新问题
   1. chroma性格库部分，增加召回时的记录，用来做淘汰等
1. 自动关闭长期未活动的会话（session timeout policy）。
5. 做成再cli命令/newsession时，触发 AttributionEngine.gun(), 注意不要和按照时间触发的代码发生竞态，也许加个锁，或者其他合适的方式。


1. 离线分析定时执行。似乎没有实现。需要确认
