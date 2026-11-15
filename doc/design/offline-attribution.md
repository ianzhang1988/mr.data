# offline-attribution：离线归因设计

> 本节定义离线归因的定位、流程、维度调整规则与跨库一致性边界。改维度规则前读 #3，改写库顺序前读 #6（两段式是折中方案，D-81）。
> 前置概念：写入的记忆形态（分片/id/修剪）见 [memory-and-retrieval.md](memory-and-retrieval.md)；在线侧产出的归因输入（维度 refs、独白、评分）见 [online-pipeline.md](online-pipeline.md)。
> 断言标注约定：`[记录]`=有 commit/文档证据；`[推断]`=从代码行为推断。

## 文档地图

1. 定位与触发 — 归因何时发生、批处理与容错
2. 归因流程 — transcript → 上下文 → LLM 归因 → 三道事后处理
3. 维度调整规则 — 强化/淘汰/核心保护/新建去重
4. 证据回写 — evidence 与 event 素材的产出与守卫
5. 会话沉淀 — 对话 → 情节记忆
6. 一致性边界 — 两段式跨库写入（折中定位）

## 1. 定位与触发

**本节回答：归因什么时候跑、一次跑多少、失败怎么办。** 离线归因是「自我修正」能力的执行体（overview#1）：复盘已关闭会话，把成败归因到性格维度。

- **触发**：仅手动——`mr-data offline` 调用 `run()`。无定时调度、无 `/newsession` 联动。 `[记录·改进 1]`
- **并发假设：单写者，无锁**。`run()` 无任何并发保护（无 flock / SKIP LOCKED）；调用方负责保证同一时刻只有一个 run 在执行。 `[记录·现状]`
- **会话生命周期现状**：会话只有 active/closed 两态，由 CLI 显式切换；不存在「长期未活动自动关闭」机制——会话不关闭就永远不会被归因。 `[记录·现状]`
- **批处理**：每次取 `offline_batch_size`（50）个「已关闭且存在未处理日志」的会话；单会话取最新 `offline_max_session_logs`（100）条未处理日志（DESC 最新优先，多批跨 run 逐批推进），**批次方向保持最新优先**（改进 82 拍板）。 `[记录·改进 82]`
- **拆批语义（改进 82，调查文档改进项 B）**：单会话未处理日志超 100 条时跨 run 拆批，四个配套机制：
  1. **上文回顾（B1）**：每批另取已处理行中按时间紧邻本批上方的 `offline_attribution_batch_overlap_lines`（2）条（单独查询，避免大窗口被已处理行占满导致深部未处理行取不到），transcript 末尾以「上文回顾」段呈现——仅供理解上下文，不重复归因、不可作 target（id 校验白名单只用本批行）；
  2. **分片死信（B2）**：归因失败达阈值后只落葬本批实际进过 transcript 的日志（`mark_dialogues_processed`），不再 session 级连坐从未取过的日志；`offline.session_dead_lettered` 的 never_in_transcript 字段保留但恒为 0；attempts 仍会话级（达阈值后后续分片失败一次即即时落葬）；
  3. **分片标注（B3）**：拆批时 system/prompt 标注「本批为第 x/y 批，非完整会话」（x = p//limit+1，y = ceil((p+u)/limit)，p/u 为会话级已处理/未处理计数；死信落葬使 y 为近似值）；未拆批时 prompt 一字不改；
  4. **会话级信号规则（B4）**：拆批时 prompt 中评分/评论段落由「最直接的成败信号，应更严格归因」替换为「背景校准 + 锚定本批事件」——评分仅用于校准判断倾向，成败计数必须有本批对话中的具体事件作证据，防止同一评分在多批被重复折算（措辞定稿见 review/truncation-survey.md「B4 prompt 定稿」）。
- **单会话容错**：LLM 归因失败或 apply 事务失败 → 计数 +1，下次 run 重试；达 `offline_max_attribution_attempts`（3）次 → 死信（**本批**未处理日志置为已处理，warning），不再无限重试。单会话失败不打断整批。 `[记录·改进 55/67/82]`
- **修剪总是执行**：`run()` 末尾统一调 `prune_stale_memories`，无待处理会话也执行（空闲期不修剪的早退 bug 已修复）。 `[记录·改进 69]`

## 2. 归因流程

**本节回答：一次会话复盘从日志到结构化结果的流水线。**

1. **transcript 构建**：日志按 (created_at, id) 排序（同刻同事务插入的两行靠 id 定序，NLP 序稳定）；行格式 `user[#12]: …` / `assistant[#13]（内心独白：…）: …`——日志 id 进 transcript 是后续校验的原料。 `[记录·改进 54/65]`
2. **上下文组装**：固定身份 + 活跃维度**按会话激活分组**（「本次会话实际激活」来自在线 `dialogue_dimension_refs` /「其余活跃」供新建去重对照）+ 用户评分与评论（未拆批时是最直接的成败信号；拆批时降级为背景校准，见「拆批语义」B4）。 `[记录·改进 51/56/82]`
3. **token 预算裁剪**：transcript 预算 = `llm_context_token_limit` − system − context − 模板常量 − 256 slack；超预算**头尾保留、中间砍**（插省略标记，整行是单位、行内不切割、[#id] 完整），截断时 system 措辞如实替换并记日志。防长文会话撑爆上下文→重试无效→死信。 `[记录·改进 75]`
4. **LLM 归因**：结构化输出（temperature=0.2），产出 deltas（每条：维度、成败增量、理由、证据片段、关系标记、事件摘要、目标日志 id）。解析失败 → 返回 None 走重试/死信。 `[记录·改进 22/33]`
5. **三道事后处理（事务外）**：新维度去重（#3）→ 按配置截断最多 3 条 → id 校验（dimension_id 须在活跃维度白名单、target_dialogue_log_id 须是本会话 assistant 行 id，非法置 None + warning——防幻觉 id 触发外键违规炸掉整批）。 `[记录·改进 53/54]`

## 3. 维度调整规则

**本节回答：一个 delta 如何改变维度集合。** 维度计数（PG `personality_dimensions` 表）与记忆召回计数（Chroma）是两套独立数字，勿混淆。

- **强化/弱化**：`delta_success`/`delta_failure` 累加到维度计数，同时写 `adjustment_logs` 审计行（含 reason、target 日志 id、event_summary）。 `[记录·改进 43/50]`
- **新建维度**：delta 无 dimension_id 且给出第一人称描述时 insert。新建是最后手段——prompt 要求先逐条对照「其余活跃维度」。 `[记录·改进 53]`
- **新建去重**：归因产出后、落库前，批量 LLM 判断候选新维度与既有活跃维度是否语义重合（从严：只有明显重合才归并）；命中则改挂既有维度；判断失败按「允许新建」降级。开关 `enable_dimension_dedup`。 `[记录·改进 53]`
- **淘汰**：非核心维度同时满足 总样本 ≥ `failure_min_samples`（10）且 失败比率 > `failure_ratio_threshold`（0.33）→ 停用 + purge 该维度向量素材（#4/#6）。比率制替代了旧的终身纯计数（成功 1000/失败 5 的优秀维度曾照淘汰）。 `[记录·改进 64]`
- **核心保护**：`core=True` 维度永不淘汰。 `[记录·改进 11/64]`
- **无复活路径**（有意）：停用维度不再参与归因，无复活信号源，实现 reactivate 判为无意义路径。 `[记录·改进 64 明确说明]`
- 单会话 deltas 上限 3 条（prompt 收敛要求 + 代码截断兜底），先去重后截断。 `[记录·改进 53]`

## 4. 证据回写

**本节回答：归因除了改计数，还给人格向量库「发育」什么。** 设计意图：归因证据与事件摘要是人格素材（line/event/evidence）的演化来源——维度不只能被计数，还能积累支撑自己的素材。 `[推断·代码内注释「发育性格向量库」]`

- **evidence**：每个证据片段一条文档——embedding 用 target 行前后窗口的上下文（`[target-2, target+1]` 四条），metadata 挂 dimension_ids、source_id=target 日志 id；PG `dialogue_vector_refs` 同步登记（doc id 在事务内预算，先于 Chroma 写入落库）。 `[记录·改进 8/61]`
- **event**：event_summary 一条文档，同上形态（context 为空则嵌入摘要本体）。
- **守卫**：无有效 target_log_id 时证据/事件不写向量库（doc id 哈希原料含 target，"None" 会污染确定性 id 与追溯链），跳过并记 info 日志。 `[记录·改进 54]`
- 文档 id 稳定派生（`evidence_doc_id`/`event_doc_id`），重放幂等，见 [memory-and-retrieval.md](memory-and-retrieval.md#2)。
- **维度淘汰 purge**：先按 PG refs 找该维度的文档删除并清 refs，再兜底扫描 personality 集合 metadata 中 dimension_ids 含该维度的残留文档。改进 62 后 ingest 台词不再关联维度（dimension_ids=[]），「淘汰误删共享素材」的现实场景已消除；evidence/event 均为单维度文档。 `[记录·改进 61/62, last_time_left #2]`

## 5. 会话沉淀

**本节回答：归因后的会话如何变成情节记忆。** 流程：会话未处理日志 → `chunk_dialogue_logs`（400 字/段、句读优先、段间 2 行 overlap、超预算行预切分共享 log id）→ 逐段 `add_memory`（稳定 id 幂等，运行态字段不被重放重置）。 `[记录·改进 31/42/66]` 分片契约细节见 [memory-and-retrieval.md](memory-and-retrieval.md#3)。

为什么由离线统一写而非在线逐轮写：在线逐条写曾产生无 source_type 标签的野数据、不参与召回计数与清理；按会话整体分段则段边界完整、metadata 齐全。 `[记录·改进 31]`

## 6. 一致性边界

**本节回答：PG 与 Chroma 两个库之间，写入的顺序与失败语义是什么。** 定位提醒：两段式是**折中方案**（D-81）——如实记录现状，不拔高为原则，也不列为待改缺陷。

### 6.1 离线：显式两段式

`_apply` 在 PG 事务内只做 PG 写（维度计数、adjustment、refs、新建维度、标记已处理），同时**收集** Chroma 副作用（待写事件、待淘汰维度）；`run()` 在事务提交成功后统一 `_flush_post_commit_chroma` 执行三类副作用，逐条 try/except 隔离。 `[记录·改进 61]`

- 回滚时零 Chroma 副作用——无孤儿向量、无误删向量（旧「确定性 id 重放自愈」语义被取代）。
- 代价（折中的一部分）：提交后 Chroma 写失败不自动重试（会话已标记 processed），残留以 warning 日志为准。 `[记录·改进 61 语义说明]`
- 事务包含「apply + mark_dialogue_processed」整体，崩溃重跑 PG 侧不翻倍。 `[记录·改进 43]`

### 6.2 在线：弱保证形态

`_log_dialogue` 的 web 记忆写入在 PG 事务块之后的顺序代码中——语义上也满足「提交后执行」，但没有显式收集/flush 结构。这是折中的另一半：两种形态并存，将来可能统一，也可能不。 `[记录·D-81]`

### 6.3 失败谱系

| 失败点 | 行为 | 依据 |
|---|---|---|
| LLM 归因/解析失败 | 计数+1，重试；达 3 次死信 | 改进 33/67 |
| apply 事务失败 | 整体回滚，会话保持未处理，重试；达 3 次死信 | 改进 43/55/67 |
| 提交后 Chroma 单条写/删失败 | 跳过该条，warning，不追溯补偿 | 改进 61 |
| prune 失败 | 返回 0，warning | 改进 38 |

## 证据区

- 改进 1/8/11/22/31/33/42/43/50/51/53/54/55/56/61/62/64/66/67/69/75（finished_improvement.md）
- 代码锚：`offline/attribution.py`（`run`/`_attribute_session`/`_dedup_new_dimensions`/`_apply`/`_flush_post_commit_chroma`）、`db/chunking.py`
- D-81（两段式折中定位）
- 推断路径说明：「发育人格向量库」意图来自 `_apply` 内代码注释；维度计数与记忆计数的区分从两套字段（PG success/failure vs Chroma recall_count）的独立消费方归纳。
