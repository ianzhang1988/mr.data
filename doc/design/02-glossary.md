# 02-glossary：术语表

> 项目专有名词与类型值的唯一定义入口。各篇按 README 规则一.2 就地一句话复述前置概念，但**定义以本篇为准**；本篇不展开机制，每条只给权威章节指针。新增专有名词/type 值时按 README 维护协议登记本篇。

## 文档地图

1. 人格与维度 —— 维度/素材/核心保护/成败增量/证据回写/淘汰
2. 记忆 —— source_type 五值/分片/召回计数/修剪
3. 在线管道 —— 状态契约/引用对账/降级兜底
4. 离线归因 —— transcript/上文回顾/死信/会话沉淀/批
5. 存储与横切 —— 两库分工/引用登记/幂等/写入边界

## 1. 人格与维度

| 术语 | 定义 | 权威细节 |
|---|---|---|
| 维度（dimension） | 性格的**结构层**：哪些性格侧面存在、本轮谁起作用；PG `personality_dimensions` 一行，在线 select_dimensions 从活跃维度中选取 | overview.md#2；offline-attribution.md#3 |
| 素材 | 性格的**参考内容层**：示例台词、归因事件与证据，存人格向量库，按语义检索 top_k 进 prompt | 01-birdseye.md 核心机制；memory-and-retrieval.md#1 |
| core 维度 | 初始化种子带 `core=TRUE` 的预置维度，永不淘汰；运行时新建维度永远非 core，无任何晋升机制 | offline-attribution.md#3 |
| active / 停用（deactivate） | 维度开关：停用的维度不参与在线选择与归因；失败率超阈即停用，**无复活路径** | offline-attribution.md#3 |
| delta（成败增量） | 一次归因产出的一条维度变化：维度 + delta_success/delta_failure + reason + evidence + event + target | offline-attribution.md#2 |
| 证据（evidence） | 归因产出的**原文摘录**（对话/内心独白逐字引用），0-N 条；嵌入用 target 行前后窗口上下文；**登记 PG refs** | offline-attribution.md#4 |
| 事件（event） | 归因产出的**一句话摘要**，0 或 1 条、可留空，记录维度被实际使用的过程；**不登记 PG refs**，仅靠 Chroma metadata 追溯 | offline-attribution.md#4 |
| 淘汰（purge） | 停用维度的清理动作：按 PG refs 删其素材文档、清 refs、兜底扫 Chroma 残留；不可逆 | offline-attribution.md#3/#6 |

**evidence vs event**：evidence 是「证明材料」——微观原文、可多可零、PG+Chroma 双链追溯；event 是「大事记」——宏观概括、最多一条、只有 Chroma 单链。

## 2. 记忆

| 术语 | 定义 | 权威细节 |
|---|---|---|
| source_type 五值 | 向量素材来源类型：`line/event/evidence`（人格库）与 `web/dialogue`（记忆库）；值域按集合划分，存储侧 Literal 强校验 | database-design.md「素材来源类型约定」（唯一权威，本篇不复制） |
| 记忆分片（chunk/片） | 记忆入库单位：dialogue/web 正文按 ~400 字/片切分，句读优先 | memory-and-retrieval.md#3 |
| 片 id（base_id） | 分片的确定性 id：URL/会话 + 内容哈希派生，重放幂等（已存在则跳过） | memory-and-retrieval.md#2 |
| recall_count | 召回计数：在线检索命中 +1 并刷新 `last_recalled_at`，修剪的免疫指标 | memory-and-retrieval.md#4 |
| 召回免疫 | `recall_count ≥ memory_min_recall_count`(3) 的记忆不因超期被修剪 | memory-and-retrieval.md#5 |
| 修剪（prune） | 记忆侧的清理：超 `memory_dialogue_retention_days`(90 天) 且低召回的记忆删除；只删 Chroma 文档 | memory-and-retrieval.md#5 |

## 3. 在线管道

| 术语 | 定义 | 权威细节 |
|---|---|---|
| DialogueState | 一轮对话在管道节点间流转的状态对象，普通 overwrite 语义（无 reducer） | online-pipeline.md#3 |
| 三态 | `selected_dimension_ids` 的语义：列表=本轮选中（可空表）、key 缺失=防御回退全量——两种「空」不得混同 | online-pipeline.md#3 |
| 内心独白（inner_monologue） | think 节点产出的推理文本，随消息落库，离线归因据此理解回答动机 | online-pipeline.md#2/#3 |
| block（引用块） | 回复的内容组织单位：每块携带引用列表（id + source_type + 一句话总结） | online-pipeline.md#5 |
| known_refs 对账 | 引用守卫：assemble 建立本轮素材清单，LLM 回传引用 id 不在清单即丢弃，source_type 以系统真值覆盖 | online-pipeline.md#5 |
| 降级 / 兜底 | 节点失败时的保守行为（宁多勿漏、保守不联网等）；总原则 D-80：LLM/检索失败不扰用户，数据写入失败不静默 | online-pipeline.md#7；decisions.md D-80 |

## 4. 离线归因

| 术语 | 定义 | 权威细节 |
|---|---|---|
| transcript | 喂给归因 LLM 的会话记录渲染：日志行 + 编号 + 内心独白，受 token 预算约束 | offline-attribution.md#2 |
| 上文回顾（overlap） | 会话拆批时紧邻已处理批上方的几行上下文，仅供 LLM 理解，不重复归因、不可作 target | offline-attribution.md#2 |
| 死信 | 重试仍失败后标记「已处理」的会话/批：失败明细留日志，防卡死处理队列 | offline-attribution.md#1/#6 |
| 会话沉淀 | 离线把会话日志分片写入记忆库（~400 字/片、哈希 id 幂等、逐片失败隔离） | offline-attribution.md#5 |
| 批（两层 LIMIT） | `offline_batch_size`(50) 限一轮 run 的会话数（closed_at 最新优先）；`offline_max_session_logs`(100) 限单会话一轮归因的日志条数（最新优先） | offline-attribution.md#1 |

## 5. 存储与横切

| 术语 | 定义 | 权威细节 |
|---|---|---|
| 两个向量库 | Chroma `personality`（人格素材）与 `memories`（记忆素材）两个集合；PG 是关系侧（日志/维度/引用） | memory-and-retrieval.md#1/#6 |
| vector_refs | PG `dialogue_vector_refs` 表：assistant 日志 → 向量素材的引用登记（历史快照语义——行不随向量删除而清理，D-88；实际只有 evidence/web 两类有行） | memory-and-retrieval.md#6；decisions.md D-88 |
| 幂等（稳定 id 派生） | 所有向量文档 id 由内容/外键确定性哈希派生，重放写入时「已存在则跳过」 | memory-and-retrieval.md#2 |
| 两段式写入 | 跨库一致性折中：PG 事务先提交，Chroma 副作用在提交后逐条 flush（Chroma 失败不回滚 PG） | offline-attribution.md#6；decisions.md D-81 |
| 单写者 | 离线归因同一时刻仅一个 run 在写：进程内锁 + PG advisory lock 双护栏 | offline-attribution.md#1 |
