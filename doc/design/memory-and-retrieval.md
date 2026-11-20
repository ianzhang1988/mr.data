# memory-and-retrieval：记忆与检索设计

> 本节定义记忆的分类学、id 派生契约、分片策略、召回计数与修剪规则。id 公式与分片参数是**契约**（改它们等于改数据形态），写入路径与读取路径是**机制**（改它们等于改管道，先读 [online-pipeline.md](online-pipeline.md)/[offline-attribution.md](offline-attribution.md)）。

## 文档地图

1. 记忆分类学 — 四类文档与两个 Chroma 集合的划分
2. id 派生规则 — 每类文档的 id 公式（契约，公式即设计）
3. 分片策略 — dialogue/web 的切分口径与句读优先原则
4. 召回与计数 — recall_count、免疫阈值、内存补丁
5. 修剪 — 过期判定、时间基准、时区假设
6. 与 PG 的协作 — vector_refs 快照语义、dimension_ids 序列化

## 1. 记忆分类学

**本节回答：向量库里存哪几类文档、为什么分两个集合。** 前置说明：持久化权威在 PG（设计原则 2），向量库只存可重算内容与辅助信号。

两个 Chroma 集合，共用同一嵌入模型（jina-embeddings-v2-base-zh，768 维）：

| 集合 | 文档类别（source_type） | 写入方 | 内容形态 |
|---|---|---|---|
| `personality` | `line` 示例台词 | `mr-data ingest` | context 整体嵌入，utterance 检索返回 |
| `personality` | `event` 人格事件摘要 | 离线归因 | 摘要文本嵌入 |
| `personality` | `evidence` 归因证据片段 | 离线归因 | 证据上下文嵌入 |
| `memories` | `dialogue` 情节记忆段 | 离线归因 | 段文本整体嵌入 |
| `memories` | `web` 世界知识片 | 在线管道 | 片正文裸嵌入 |

划分理由：personality 集合的文档参与「性格塑造」、带 dimension_ids 且会被维度淘汰 purge；memories 集合参与「上下文回忆」、带召回计数且会被 prune。生命周期管理策略完全不同，故分集合。

五值 source_type 的完整约定（含义/写入路径/读取路径）是数据层契约，见 [database-design.md](../database-design.md)「素材来源类型约定」，本文不重复；术语入口见 [02-glossary.md](02-glossary.md#2)。

**嵌入文本与存储文本**：personality 集合的 line/event/evidence 以「上下文+内容」中较长者嵌入（`event.context or event.content`），文档原文存储即嵌入文本；检索返回时 `page_content` 取 utterance（台词本体），完整 context 留在 metadata。

## 2. id 派生规则

**本节回答：每类文档的 id 怎么算——这是幂等契约的核心。** 所有 id 由 `db.chroma::_stable_doc_id(kind, *parts)` 统一派生：`sha256("\x1f".join(parts))`，带 kind 前缀防跨类型碰撞。

| 类别 | id 公式（kind + 哈希原料） | 稳定性语义 |
|---|---|---|
| line | `line:` sha256(speaker, content, context) | 同台词重复 ingest 跳过 |
| event | `event:` sha256(target_log_id, dim_id, summary) | 归因重放不重复 |
| evidence | `evidence:` sha256(target_log_id, dim_id, snippet) | 同上 |
| dialogue 段 | `dialogue:` sha256(session_id, first_log_id, last_log_id, content) | 会话重归因不重复 |
| web 片 | `web:` sha256(url, 全文) + `:c{i}` 片序号 | 同页重抓正文不变→片界稳定→原地刷新；正文变→全新 id 无孤儿 |

写入语义统一为「传稳定 id → 已存在则跳过」（`add_personality_event`/`add_memory`），跳过时不重置 recall_count/added_at 等运行态字段。

web 片 id 的设计要点：

- 哈希原料是 **URL+全文** 而非仅 URL——正文变化导致片界漂移时，仅 URL 哈希会让新旧片互相覆盖成孤儿；URL+全文则让正文变化表现为全新 id 集，旧片由 prune 自然消亡。
- 「全文」= 抽取层填充的 `WebDoc.original_content`（纯抽取正文，不含 title；改进 83 起）。**哈希基准必须是 query 无关的原文**：LLM 摘取版随问法变化，做基准会因 base_id 漂移积累重复分片。无原文的退化路径（snippet 透传/抽取失败）回退 page_content。
- 片序号 `:c{i}` 从正文切分自然派生，切口稳定（见 #3）故序号稳定。
- 存量旧单文档 web 记忆（改进 76 之前）不迁移，靠 prune 自然消亡；改进 83 哈希基准切换后，存量按摘取版哈希的 web 片同样一次性失配、重新写入一轮，过渡期新旧并存至旧套 90 天 prune 消亡（检索噪音翻倍，无害）。

web **管道文档**（web 子管道内流转的 WebDoc）身份即 metadata.url，无独立 id——勿与片 id 混淆：前者是管道引用键，后者是落库幂等键。

**调用方未传 id 时退化为 uuid4** 且不查重——这是兼容路径，新写入路径必须传稳定 id（校验信息即约定）。

## 3. 分片策略

**本节回答：为什么要切、按什么口径切。** 嵌入模型在 fastembed 内部按模型自带 tokenizer 的 `model_max_length` 静默截断（jina v2 为 512 token）——超窗文本段尾被无声砍掉，成为检索死重。因此：**任何入库文本的字符预算必须 ≤ 嵌入窗口**，由启动校验强制（`memory_*_chunk_chars > embedding_max_tokens` 拒绝启动）。

切分口径：字符预算 400（≈窗口 75%，保守按中文 1 字≈1 token，余量容纳段首 overlap），切口「句读优先」——预算窗口内最后一个 `。！？\n` 处下刀、标点留前段；窗口内无句读硬切。

| | dialogue 段 | web 片 |
|---|---|---|
| 预算 | `memory_dialogue_chunk_chars=400` | `memory_web_chunk_chars=400` |
| overlap | 2 行（约一轮对话） | 40 字符（≤10%） |
| 单行/片超预算 | 行内预切分，片段共享同一 log id | 不适用（正文连续） |
| 写入方 | 离线归因（会话整体） | 在线管道（每轮逐片） |
| overlap 语义 | 段首 overlap 行不计入字符预算（实际嵌入文本略超预算，400/512 余量覆盖） | 下一片起点回退 overlap 后推进到最近句读，保证切口稳定、循环前进 |

**overlap 的定位与对标**：overlap 是防「跨边界信息丢失」的保险——跨两片的一句话保证至少一片完整；形态为业界滑窗标准（下一片头复制上一片尾，与 LangChain `chunk_overlap` 同构），非自定义发明。参数对标业界基准 10-20%（NVIDIA 在 FinanceBench 实测 15% 最佳）：web 40 字/400 字 = 10%，压基准下沿；dialogue 2 行低于基准线，属刻意保守——行级粒度保证「整轮对话」不被切断（对应「整句重叠优于裸字符重叠」的推荐），代价是边界覆盖略薄。重叠内容重复计存储与嵌入成本、>20% 后收益递减（检索结果重复、稀释 top-k 多样性），故不取更高值；待积累检索日志后可按「相邻片同进 top-k 频率」实证再标定。

**正文不变 ⇒ 片界/段界不变**是幂等的前提：两句读间距离在两次切分间不变，故同一份文本永远切出同样的片序列。

## 4. 召回与计数

**本节回答：检索命中后系统记什么、这些数字驱动什么。**

- 在线 `_retrieve_memories` 命中后统一调 `increment_memory_recall`：dialogue 与 web 都计数（recall_count+1、刷新 last_recalled_at）。
- **写回按返回 ids 对齐（Chroma 顺序契约）**：`get(ids=[...])` 返回的是存储序，与请求序无关——任何「按位置把返回的 metadata 配对回请求 id」的写法都是 bug（字段张冠李戴）。**做法**：配对一律以返回的 `ids` 数组为键（`increment_memory_recall` 即以 `found_ids` 对齐写回的范例，新写入路径照此办理）；请求中存在但返回中缺失的 id 跳过并 warning（`chroma.recall_increment_missing_ids`），不抛异常打断对话。
- **双路径实现**：dialogue 经 `DialogueMemoryMetadata` 模型读-改-写；web 的 recall_count/last_recalled_at 是 `WebMemoryMetadata` 的 extra 字段，走裸 dict 容错路径。功能等价，统一化未做（实现卫生项，非语义问题）。
- **内存补丁**：计数写库后，state 内文档的 recall_count/last_recalled_at 就地 +1 刷新，保证下游 prompt 渲染与 pruning 看到的是新值。补丁范围以 `increment_memory_recall` 返回的 `found_ids`（实际命中者）为准——已被删的 id 不在补丁集合内，不重复 +1。
- 计数失败的降级：recall 计数是辅助信号，increment 失败只 warning，已检索到的记忆照常参与本轮对话。

这些数字驱动修剪（#5）；维度计数（success/failure）是 PG 侧的另一套数字，驱动维度淘汰，见 [offline-attribution.md](offline-attribution.md#3)，勿混淆。

## 5. 修剪

**本节回答：什么记忆会被删、凭什么判过期。**

`prune_stale_memories` 在每次离线 `run()` 末尾执行（无待处理会话时也执行，改进 69 修复了早退跳过）。

删除条件（两个同时满足）：

1. **超期**：时间基准 dialogue 取 `last_recalled_at` 回退 `added_at`；web 取 `last_recalled_at` 回退 `retrieved_at`；距今超过 `memory_dialogue_retention_days`（90 天，dialogue/web 共用，配置名保留 dialogue 前缀兼容存量 .env）。
2. **低召回**：recall_count < `memory_min_recall_count`（3，即「召回免疫阈值」）——被反复用到的记忆不因超期被删。

宽容语义：metadata 无法解析、时间字段缺失/坏的条目**跳过不删**（宁留勿误删）；naive 时间戳按 UTC 解读。

修剪失败整体 try/except 记 warning 返回 0，不打断离线批处理。

## 6. 与 PG 的协作

**本节回答：向量库与结构数据之间靠什么表互相对账。**

- `dialogue_vector_refs`：素材引用登记表，**设计契约：记录本轮进入 prompt 的全部素材**——人格素材（按 doc）、记忆素材 memory_docs（dialogue 段按段、web 旧片按片）、本轮新沉淀 web 片（按片）；每行 = `vector_doc_id`（出处指针，目标可能已被 prune 删除）+ `content`（原文快照，行的价值所在）+ source_type + dimension_ids。离线归因的 evidence 也登记（指向 target 日志）。注意登记发生在 retrieve 阶段，是「实际检索到」而非「实际被引用」的审计链。
- `dialogue_dimension_refs`：在线每轮实际选中的维度；离线用它区分会话激活维度。
- dimension_ids 在 Chroma metadata 落库为 CSV、读出还原为 list[int]，非数字 token 跳过并 warning（脏数据容错）。
- **一条 ref 应包含的数据**：每行登记 = ① `vector_doc_id`——出处指针（Chroma 文档 id 或分片 id），目标可能已被 prune 删除，指针失效不追删行；② `content`——原文快照，行的价值所在（prune 后仍可审计当时进 prompt 的是什么）；③ `source_type`——五值之一（line/event/evidence/web/dialogue），集合归属与写入路径的判别依据；④ `dimension_ids`——素材关联的维度，人格素材带维度、记忆素材恒空（落库 NULL，天然不参与维度 purge）；⑤ 与所属 `dialogue_log_id` 同事务写入，崩溃整体回滚不留半登记。粒度约定：人格素材按 doc、记忆与新沉淀 web 按分片；同一轮内 `vector_doc_id` 去重（旧 web 片被召回 + 同页重抓正文未变时分片 id 相同）。登记发生在 retrieve 阶段，记录「实际检索进入 prompt」而非「实际被 LLM 引用」——回复引用链由 ReplyBlock.references 承担。
