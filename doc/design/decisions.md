# 决策日志（decisions）

> 每条决策的裁决记录。问答批直接标 Q 编号（见 qa-pending）。
> 格式：问题 / 结论 / 依据 / 影响。

---

## D-80 降级不对称立为设计原则

- 日期：2026-09-13
- 问题：LLM/检索节点失败一律保守降级，唯独 PG 写入失败穿透到用户——是有意设计还是历史累积？（Q-1）
- 结论：A——有意设计，立为正式原则：「LLM/检索失败保守降级绝不影响用户；对话数据写入失败不可静默，允许整轮失败」。
- 依据：用户答复 Q-1（选 A）；改进 64/65 补丁方向一致
- 影响：overview#3 原则、online-pipeline#7 降级链总表

## D-81 跨库两段式是折中方案，如实记录

- 日期：2026-09-13
- 问题：「PG 事务内不执行 Chroma 副作用」能否立为全局原则？在线 web 记忆写入是例外还是偏差？（Q-2）
- 结论：折中方案——忠实记录现状的折中性质：离线是显式两段式（`_apply` 收集 + 提交后 flush），在线是「PG 事务块之后顺序执行」的弱保证形态；不做原则拔高，也不列为待改缺陷。将来可能优化，也可能不。
- 依据：用户答复 Q-2（非选项，自定义答复）
- 影响：offline-attribution#6 与 online-pipeline#7 各自如实写现状形态与折中定位

（Step 2 各篇 Q 批归档于此，追加在下方）

## D-88 refs 行是历史完整记录，不做 prune 连带清理

- 日期：2026-09-13
- 问题：记忆被 prune 删除后，`dialogue_vector_refs` 中指向已删文档的行变悬空——要不要级联删行？
- 结论：不清理。refs 定义为「历史过程的完整记录」：行的价值由 content 原文快照承担，`vector_doc_id` 只是出处指针而非存活对账键；级联删除只增两库耦合与写路径，无信息收益。
- 依据：用户拍板——「既然保留了原文，清理没有任何实际意义，反而是负担；语义上 PG 是历史过程的完整记录」
- 影响：① 消费方按 `vector_doc_id` 跳转前必须容忍目标不存在（防御姿态同 Chroma 存储序契约）；② prune 无需反向清 PG；③ 行的生命周期跟随所属 assistant 日志（FK ON DELETE CASCADE），不被 prune 提前清除；④ memory-and-retrieval #6 的「对账断裂」定性撤销，memory_docs 缺口收窄为「未登记」本身

## D-89 标注系统随审核完成退役

- 日期：2026-09-13
- 问题：README 规则一.4（每篇文末「证据区」）与规则三末行（每篇头部断言标注约定行）是否继续保留？（Q-4）
- 结论：A——标注系统（`[记录]`/`[推断]`/证据区）整体退出 doc/design：删除 README 两条规则，九篇文档维持无标注形态，新文档/新编辑不再要求标注纪律。
- 依据：用户答复 Q-4（选 A）；标注是审核期脚手架，审核已完成、目的耗尽；证据链由 finished_improvement.md 改进号、decisions.md D 号、git 历史保留，「为什么」由正文中的纯文字改进号引用承担（如「改进 87 由 0.33 上调」）
- 影响：① README 删规则一.4 与规则三末行、验收清单去标注残留；② 台账层（finished_improvement/decisions）继续作为证据权威，职责不变

## D-90 允许人工复活（重置计数）与删除已停用维度，取代「无复活路径」

- 日期：2026-11-17
- 问题：offline-attribution#3 原设计「停用维度不再参与归因，无复活信号源，reactivate 判为无意义路径」——引入人工维度编辑后是否维持？
- 结论：推翻原设计。维度编辑模式（chat /dimension、CLI mr-data dimension）允许人工 reactivate——人就是复活信号源；复活必须同时清零 success/failure 计数，否则旧计数（如 7 败/10 样本，比率 > 0.6 阈值）会在下一轮归因立即再次触发淘汰，复活形同虚设。同时允许删除已停用维度：双重确认（y/N + 重输 id），清理 adjustment_logs 行（FK 无级联动作，阻断删除）、dialogue_dimension_refs（FK 级联自动）、Chroma 向量残留（与引擎 purge 同模式，复用同一函数）。core 维度在交互层与 store 层双重拒绝，永不淘汰语义延伸为全拒。
- 依据：doc/dimension-editing.md v4 定稿（用户裁决：状态化模式、全命令单斜杠、变更写审计日志）
- 影响：① offline-attribution#3「无复活路径」条目改写为「人工复活与删除」；② 变更操作写审计日志（dimension.edit/deactivate/reactivate/delete，logs/ 应用日志，不建审计表）；③ 新建维度仍唯一归引擎自动发现，计数唯一合法变更路径是复活重置


## D-91 chat HTTP API 默认端口取 8080

- 日期：2026-11-17
- 问题：chat HTTP API 的 `server_port` 默认值用设计稿提出的 8377 还是另指定？（Q-5）
- 结论：取 **8080**。`server_host` 默认 `127.0.0.1` 不变。
- 依据：用户答复（非选项，直接在 plan 文档 §11 批注「默认值 8080」）
- 影响：doc/chat-http-api.md §7 配置默认值；实施时 config.py 按此默认值

## D-92 v1 即暴露 `POST /offline`，同步等待归因完成

- 日期：2026-11-17
- 问题：chat HTTP API 是否暴露手动触发离线归因端点？若加，是否等待归因完成再返回？（Q-6）
- 结论：**v1 就加**。语义：请求线程内执行 `guarded_offline_run(source="http")` 并**同步等待完成**——agent 测试需要关会话后立即断言归因效果（维度计数、证据回写），异步触发会造成断言竞态。返回映射：`completed`→200 `{"status":"completed"}`；`busy`/`locked`→409 `offline_busy`；`failed`→500 `offline_failed`。护栏不变：与 cron/手动 CLI//newsession 完全互斥（双层锁），HTTP 只是互斥触发源新增一个通道，日志事件以 `offline.http_*` 命名。
- 依据：用户答复（直接在 plan 文档 §11 批注「加上吧，让 agent 测试的能力完整」）；同步语义为实现方按「测试能力完整」目标所定，已在 plan §3.7 写明
- 影响：doc/chat-http-api.md §3.1 错误码表、§3.2 端点表、新增 §3.7；实施时 server 端点与测试用例按此契约
