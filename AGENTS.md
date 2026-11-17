# AGENTS.md — mr.data

## 设计文档

设计细节以 `doc/design/` 为准（project_object.md 是目标层，doc/database-design.md 是表结构层）。
查询动线：先 `doc/design/00-index.md` 路由表定位到「篇名#节号」，只取该节阅读；
首次接触或跨领域任务先读 `doc/design/01-birdseye.md`。禁止整篇通读除非任务需要。
改代码动了设计契约（状态 key、id 派生、降级方向、维度规则）时，同步更新对应节；
改完自查 `doc/design/00-index.md` 的路由是否仍然指向正确章节。

## 测试

注意原则 doc/testing-guidelines.md

```bash
.venv/bin/python -m pytest tests/ -q
```

**预期耗时与超时设置（重要，避免误判超时被杀）**：

| 场景 | 预期耗时 | 建议 timeout |
|---|---|---|
| 全量测试（热：PG 集群已存在） | 40–50s | **≥ 300s** |
| 全量测试（冷：首次/schema 变更需 initdb） | 60–70s | **≥ 300s** |
| 机器负载高时 | 可能放大到 3–5 倍 | 用 600s |

- Shell 工具默认 timeout 是 60s，**跑全量测试必须显式设置 timeout ≥ 300s**，否则会被误杀。
- 单文件测试可能仍需支付 pgembed 启动（热 ~0.1s / 冷 ~15s）。

## 测试基建要点

- PG：session 级 `pgembed_server` 复用固定集群 `/tmp/mr-data-pgembed-test-<uid>`（schema 指纹不符或启动失败会自动删目录重建）；每用例前 `reset_pg_state` 清表 + seed。
- Chroma：`chroma_store` fixture 用 `ChromaStore(ephemeral=True)` 内存模式；EphemeralClient 是进程级单例，init 时已做集合清理保证隔离。
- LLM：`FakeLLMClient`（conftest.py），无网络调用。

## 工作流约定

- 改进任务由 `need_improve.md` 驱动；完成后更新该文件与 `finished_improvement.md`。
- **提交前需用户明确指示**；`need_improve_tmp_dont_delete_change.md` 与 `review/` 永不提交（`review/do_review.md` 除外，该文件已跟踪、需要提交）。
- 日志统一用 `mr_data.logging.get_logger(name)`，结构化 extra 只含 `event`/`session_id`/`details`。
