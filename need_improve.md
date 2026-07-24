# 在改进前，首先
* 使用 scripts下的code_struct.py 获取src目录下的代码结构

# 加入以下prompt，这是我对你的要求
- 从待改进部分，去读当前的任务，做必要的分析
  - 首先分析是否合理，有没有重大的脱离当前项目结构的问题，如果有与我交流确认。没有就继续。
  - 计划要做的事情，分成具体的步骤内容。
  - 如果可以并行完成，则使用子agent来加速生产速度。
- 完成修改后，更新本文档

# 待改进

1. 在 DialogueState 里加 messages, 保留最近 10 轮对话(默认值，配置中增加配置项)。
  - 保留用户输入, agent助手的输出, inner_monologue
  - 保留已完成修改中的id和内容概括在到messages中（agent后续可以自己去取，后续计划，先保留数据，不做对应功能）。
  - 入库的不需要保留。
2. graph.py 中_assemble_and_generate 考虑到模型的ctx的上下文大小, 需要有一定的处理方法。
  - 增加一个本地Tokenizer，找个常用有代表性的。用于估算目前整体给llm的数据的大小
  - 再配置中增加一个token的限制（默认30K），如果组装的内容经过toenizer计算超过了配置，调用llm去提取后面的内容，把数据压缩到限制内
  - 另外原来的 代码中 记忆内容和web内容，放到system prompt本事似乎就不合适。你判断下是不是我上面提到的顺序更好
  - 组装组装顺序我认为应该大致如下
    - 系统提示
    - 核心性格，身份
    - 用户输入
    - 性格向量库内容
    - 记忆向量库内容
    - DialogueState 里 messages
    - web 内容
  - 超限制时，从可以从最后面的项目开始往前，调用llm压缩，注意这里调用也要保证在token限制内
  - 你考虑下，是保持每个项目中的列表，然后一个一个调用llm压缩，然后计算限制好。还是组装完成后，读取后面的数据让llm去压缩好。
    - 我觉得组装完成后可能简单，但是截断处的语义可能有问题。也许需要一些辅助，例如让llm先做语义的截断，再去压缩。

# 已完成的改进项
(保留最近项目，完成项目放到finished_improvement.md中)

25. ✅ **`_assemble_and_generate` 结构化输出与参考来源**：`AssistantReply`/`ReplyReference` 模型定义回复文本与参考列表；生成时要求 LLM 输出每条参考的 id、source_type 和一句话 summary；CLI 新增 `--show-references`/`show_references` 配置控制是否显示参考来源。
26. ✅ **Web 搜索结果长期稳定 id**：`_to_doc_format` 改用 URL SHA256 哈希作为 doc id；新增 `ChromaStore.upsert_memory`，`_log_dialogue` 写入网络资料时使用稳定 id 并作为全局知识（`session_id=""`），避免 `web:0/web:1` 每轮重置导致的冲突。
27. ✅ **DialogueState 保留最近 N 轮 messages**：`dialogue_logs` 新增 `metadata` JSONB 字段，用于保存 assistant 回复的 `inner_monologue` 和 `reply_references`；每轮启动时从 Postgres 加载最近对话构造 `DialogueMessage` 列表；`DialogueState` 新增 `messages` 字段与 reducer；配置项 `dialogue_state_message_turns` 默认 10。
28. ✅ **`_assemble_and_generate` Token 预算与提示词结构**：引入 `tiktoken` 实现 `TokenCounter`；新增 `llm_context_token_limit`（默认 30K）与 `tokenizer_model` 配置；按优先级组织 prompt section：system / identity+核心性格 / 用户输入 / personality / memory / messages / web / format；超限时按优先级从低到高压缩，低优先级内容调用 LLM 摘要或硬性截断。进一步将上下文材料从 system prompt 拆分到 `assistant` role 消息中，system 首段明确指引 LLM 如何使用人格/记忆/对话/web 素材；assistant 消息中每类素材使用 XML 标签（`<personality>`/`<memory>`/`<messages>`/`<web>`）包裹，便于模型识别分类。

# 未来可选增强(计划中)

- 自动关闭长期未活动的会话（session timeout policy）。
- 更完善的日志查看/搜索 UI 或 CLI 命令。
2. WebRelevanceItem 当内容整体与用户输入相关时，中增加对用户输入相关内容的摘取。减少token使用
2. 最后对话组装和生成的部分，是不是考虑做个独立codeagent？给他获取相关内容的能力
1. 考虑 _log_dialogue 中对话加入记忆向量库的部分，做成再cli命令/newsession时，加入整个session的对话。是不是对保留上下文，和取回都更好。
