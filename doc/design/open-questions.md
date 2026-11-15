# 未决问题（open-questions）

> **警告：本文件不是任务清单。** 列出的问题均未决定是否要修——未获用户明确要求前，禁止实现/修改其中任何一项。这里只是「现状≠设计」的备注层，供理解代码时参考。
> 答复后转 decisions.md 并在对应文档把 `[待确认]` 改 `[记录]`。
> 编号 OQ-NN 永续追加。
> 状态图例：待裁决 = 等用户决定是否修；已知现状 = 无待裁决，仅记录「现状≠设计」。

## OQ-01 assistant_log_id 入 DialogueState 后无人读（待裁决）

- 提出：2026-09-13，online-pipeline#3 撰写时发现
- 问题：`log_dialogue` 把 `assistant_log_id` 写进 state，但无后续节点读取（审计 C-4）。是预留接口还是死字段？
- 候选：删字段 / 保留作调试锚点（落库已有 RETURNING，state 冗余）
- 状态：待裁决

## OQ-02 dialogue_vector_refs 记录检索全集而非实际引用（已知现状）

- 提出：2026-09-13，online-pipeline#5 / memory-and-retrieval#6 撰写时发现
- 问题：该表语义是「本轮检索到的全部素材」，memory_docs 完全缺席；记忆被清理后引用悬空，追溯链断裂（last_time_left #14）。
- 候选：改为「实际被引用的素材」需 blocks 与 refs 对齐改造；扩表加 memory 行
- 状态：已知现状，修复与否待裁决

## OQ-03 upsert_memory 生产路径零调用（待裁决）

- 提出：2026-09-13，memory-and-retrieval#7 撰写时发现
- 问题：`ChromaStore.upsert_memory` 幂等语义被 add_memory（稳定 id + 跳过）取代后仅剩测试使用（改进 76 范围外建议）。
- 候选：删除 + 同步删其单测 / 保留作公共 API
- 状态：待裁决

## OQ-04 检索日志 query 字段张冠李戴（已知现状）

- 提出：2026-09-13，online-pipeline 撰写时发现
- 问题：三个检索节点实际用各自 query（personality_query/memory_query/search_query），日志 details 统一记 `retrieval_query`（=memory_query），排障误导（last_time_left #11）。
- 状态：已知现状，修复与否待裁决

## OQ-05 代码中对 list 的截断是否都合适（待调查）

- 提出：2026-09-13，need_improve.md 未来增强清单 #9 归属
- 问题：多处 `[:N]` 截断（web 提取、transcript、deltas 等）未逐一审计合理性。
- 状态：待调查（审计类任务，非设计问题）
