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
（保留最近三条，完成的条目放到 finished_improvement.md 中归档；35-84 已迁移归档）

98. ✅ 低风险批：review/low_risk_triage.md「范围小」16 条（F-2/11/12/13/16/18/19/20/22/23/24/27/28/29/30/31）全部核验属实并修复，triage 文件已逐条标记完成。详见 finished_improvement.md #98。

97. ✅ /newsession 三步（问评分→关旧会话→建新会话）逐项 try/except 保护：评分失败照记日志继续 close→create，close/create 失败提示后保持状态自洽，对话不再因单步数据库抖动整体退出。详见 finished_improvement.md #97。

96. ✅ build_runtime 拆兜：引擎构造失败独立记 offline.engine_init_failed（不再误报成 cron 串非法），cron 校验单独兜 ValueError；TUI 可见行为逐字不变。详见 finished_improvement.md #96。

95. ✅ message 端点错误契约完整履约：body 可选（缺体/null → 400 invalid_message），新增全局 RequestValidationError 处理器（解析层校验失败 → 422 统一形状新 code invalid_request）；契约文档 §3.1/§3.5 同步。详见 finished_improvement.md #95。

# 未来可选增强（计划中）

2. 更完善的日志查看/搜索 UI 或 CLI 命令。
3. graph.py 中考虑到本地运行使用的模型，例如qwen3.5:9B，目前部分结构化输出对模型的压力可能太大了。我们需要一种兼容性的模式，根据配置来决定是否用复杂的结构化输出方式。（改进项 37 已落地 `llm_structured_mode` 配置与自动降级，剩余：复杂 schema 本身的精简，如块级引用嵌套结构的简化模式）, 我说的其实是model不能完成正确的json输出，甚至连格式都不能保证的情况下。要如何降级的问题。改进37没有处理这类问题的能力。
4. 最后对话组装和生成的部分，是不是考虑做个独立codeagent？让他有更好的思考完成任务的机会，可以考虑再加上获取相关内容的能力
