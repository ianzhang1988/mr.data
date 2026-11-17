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

（当前无待改进项）

# 已完成的改进项
（保留最近三条，完成的条目放到 finished_improvement.md 中归档；35-84 已迁移归档）

91. ✅ 测试套件对照 review/testing-guidelines-audit.md 全量修复（P0+P1+P2）：外部 DSN 防护、日志穿透双修复（reset_loggers 摘 handler + autouse 日志隔离，真实日志零写入）、11 处恒真断言改 `reply.text`、seed fixture 化、补 reply_fallback 中间层用例（并暴露修复 OfflineScheduler.next_fire 真实 bug）、死测试门控、chroma_backup 共享目录、文案/模板断言收敛（web_filter 常量提取等）、importorskip 53 处收敛、_PG_BUSINESS_TABLES 守护、e2e 归位。全量 262 passed(+3), 1 skipped / 144s。详见 finished_improvement.md #91。

89. ✅ PostgresStore 连接池：`_cursor`/`transaction` 改走 psycopg_pool（按 dsn 进程级共享池 min_size=1/max_size=8），消除每轮十余次 connect/close；全量测试 118.46s → 92.86s（-22%）。详见 finished_improvement.md #89。

90. ✅ 性格维度编辑交互（维度编辑模式）：chat `/dimension` 与 CLI `mr-data dimension` 进状态化模式，`/list`（分页20行）`/edit`（编号选择）`/deactivate` `/reactivate`（复活清零计数）`/delete`（双重确认+清理链），变更写审计日志；core 双保险拒绝。详见 finished_improvement.md #90。

# 未来可选增强（计划中）

2. 更完善的日志查看/搜索 UI 或 CLI 命令。
3. graph.py 中考虑到本地运行使用的模型，例如qwen3.5:9B，目前部分结构化输出对模型的压力可能太大了。我们需要一种兼容性的模式，根据配置来决定是否用复杂的结构化输出方式。（改进项 37 已落地 `llm_structured_mode` 配置与自动降级，剩余：复杂 schema 本身的精简，如块级引用嵌套结构的简化模式）, 我说的其实是model不能完成正确的json输出，甚至连格式都不能保证的情况下。要如何降级的问题。改进37没有处理这类问题的能力。
4. 最后对话组装和生成的部分，是不是考虑做个独立codeagent？让他有更好的思考完成任务的机会，可以考虑再加上获取相关内容的能力
1. 写个计划检查所有的测试项目，是否有可以合并的，失效的，或者意义不大的等等。目标是简化测试集。另外，注意测试使用的时间，例如sleep(1)这样，除了数据库网络等，超过100ms的测试就比较可疑。
1. 增加chat http交互api，这样后续可以用agent测试agent，先不要改项目，考虑设计。
