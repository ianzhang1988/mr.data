# 设计文档索引（路由表）

> 用法：带着问题来，按表定位到「篇名#节号」，只取该节阅读；首次接触或跨领域任务先读 [01-birdseye.md](01-birdseye.md)。禁止整篇通读除非任务需要。
> 维护本目录（改任何文档/新增篇章）前必读 [README.md](README.md) 的规则。
> 本文档随撰写持续回填；章节编号稳定，新增条目追加、不重编号。

## 路由表

| 我想…… | 去读 |
|---|---|
| 快速了解系统全貌 | [01-birdseye.md](01-birdseye.md) |
| 查项目专有名词/类型值定义 | [02-glossary.md](02-glossary.md) |
| 新接手项目，建立概念框架 | [overview.md](overview.md#2) 核心概念 |
| 改动某个管道节点的行为 | [online-pipeline.md](online-pipeline.md#2) 节点职责卡 + #7 降级链 |
| 新增/修改 DialogueState 的 key | [online-pipeline.md](online-pipeline.md#3) 契约表 + #2 相关节点 |
| 搞清某个状态 key 谁写谁读 | [online-pipeline.md](online-pipeline.md#3) DialogueState 契约表 |
| 调整 prompt 分节/优先级/预算 | [online-pipeline.md](online-pipeline.md#4) prompt 组装 |
| 改引用、blocks、known_refs 逻辑 | [online-pipeline.md](online-pipeline.md#5) 引用与可解释性 |
| 新增搜索 provider / 改 web 子管道 | [online-pipeline.md](online-pipeline.md#6) web 子管道 + [memory-and-retrieval.md](memory-and-retrieval.md#2) 片 id |
| 新增一类记忆（新 source_type） | [memory-and-retrieval.md](memory-and-retrieval.md#1) 分类学 + #2 id 派生 + #3 分片 + database-design.md「source_type 约定」 |
| 改记忆分片预算/切口逻辑 | [memory-and-retrieval.md](memory-and-retrieval.md#3) 分片策略 |
| 改 recall 计数/免疫阈值/修剪 | [memory-and-retrieval.md](memory-and-retrieval.md#4) 召回计数 + #5 修剪 |
| 改向量库与 PG 的对账表 | [memory-and-retrieval.md](memory-and-retrieval.md#6) 与 PG 的协作 |
| 改归因 prompt / transcript / 预算 | [offline-attribution.md](offline-attribution.md#2) 归因流程 |
| 改维度淘汰/新建/去重规则 | [offline-attribution.md](offline-attribution.md#3) 维度调整规则 |
| 改证据/事件回写 | [offline-attribution.md](offline-attribution.md#4) 证据回写 |
| 改写入顺序/事务边界/重试死信 | [offline-attribution.md](offline-attribution.md#6) 一致性边界 + [online-pipeline.md](online-pipeline.md#7) 降级链 |
| 为什么当初这样设计 | [decisions.md](decisions.md) 按 D 编号检索 |

## 全局约定速查

| 约定 | 详情在哪 |
|---|---|
| 素材来源类型 source_type 五值（line/event/evidence/web/dialogue） | [../database-design.md](../database-design.md)「素材来源类型约定」 |
| Chroma 写入幂等（稳定 id + 已存在跳过） | [../database-design.md](../database-design.md)「Chroma 写入幂等约定」+ [memory-and-retrieval.md](memory-and-retrieval.md#2) |
| PG 表结构 | [../database-design.md](../database-design.md) |
| 项目目标、原则、非目标 | [overview.md](overview.md#1) / #3 / #4 |
| 降级总原则 | [overview.md](overview.md#3) 原则 6（D-80） |
| 跨库写入顺序 | [offline-attribution.md](offline-attribution.md#6)（D-81 折中定位） |
| 项目专有名词/type 值唯一定义 | [02-glossary.md](02-glossary.md) |
