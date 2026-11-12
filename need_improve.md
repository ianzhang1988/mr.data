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

1. 优化代码中截断的地方, 参考调查文档 mr.data/review/truncation-survey.md, 文档中标题 改进方案 中有多个独立改进项，每次执行性一个项目的修改, 项目修改完成时在文档中标注一下完成，本次修改项: A
2. chat_structured 主路径可改用 message.pagsed 替代 json.loads

# 已完成的改进项
（保留最近三条，完成的条目放到 finished_improvement.md 中归档；35-76 已迁移归档）

77. ✅ 历史轮 token 预算 base 双扣修复：历史窗口循环条件 used（含 base）与 remaining（已扣 base）比较导致 base 扣两次、历史窗口缩水一个 base 量级；改 `turn_tokens > remaining` + `remaining -= turn_tokens`。详见 finished_improvement.md #77。

78. ✅ web 引用 id 改 URL：prompt 素材行与 known_refs 键改用 metadata.url（引用即链接），extract 的 id 重写与 `_stable_web_id`/`_is_redirect_url` 删除，入库管道不动。详见 finished_improvement.md #78。

79. ✅ WebDoc 删除独立 id 字段，文档身份即 metadata.url：prompt 素材行经 `_render_web_doc` 渲染（无 url 退 `[web]` 标记且不注册引用），`_to_doc_format`/`web_filter` 相应简化。详见 finished_improvement.md #79。

# 未来可选增强（计划中）

2. 更完善的日志查看/搜索 UI 或 CLI 命令。
3. graph.py 中考虑到本地运行使用的模型，例如qwen3.5:9B，目前部分结构化输出对模型的压力可能太大了。我们需要一种兼容性的模式，根据配置来决定是否用复杂的结构化输出方式。（改进项 37 已落地 `llm_structured_mode` 配置与自动降级，剩余：复杂 schema 本身的精简，如块级引用嵌套结构的简化模式）, 我说的其实是model不能完成正确的json输出，甚至连格式都不能保证的情况下。要如何降级的问题。改进37没有处理这类问题的能力。
4. 最后对话组装和生成的部分，是不是考虑做个独立codeagent？让他有更好的思考完成任务的机会，可以考虑再加上获取相关内容的能力
8. 考虑性格向量库的更新问题
   1. chroma性格库部分，增加召回时的记录，用来做淘汰等
1. 自动关闭长期未活动的会话（session timeout policy）。
5. 做成再cli命令/newsession时，触发 AttributionEngine.gun(), 注意不要和按照时间触发的代码发生竞态，也许加个锁，或者其他合适的方式。


1. 离线分析定时执行。似乎没有实现。需要确认

1. 优化代码中截断的地方, 参考调查文档 mr.data/review/truncation-survey.md, 文档中标题 改进方案 中有多个独立改进项，每次执行性一个项目的修改, 项目修改完成时在文档中标注一下完成，本次修改项: B
