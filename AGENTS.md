# AGENTS.md — mr.data

## 测试

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
- 基线：**78 passed, 1 skipped**（skip 的是需下载真实 embedding 模型的手动测试）。
- 单文件测试可能仍需支付 pgembed 启动（热 ~0.1s / 冷 ~15s）。

## 测试基建要点

- PG：session 级 `pgembed_server` 复用固定集群 `/tmp/mr-data-pgembed-test-<uid>`（schema 指纹不符或启动失败会自动删目录重建）；每用例前 `reset_pg_state` 清表 + seed。
- Chroma：`chroma_store` fixture 用 `ChromaStore(ephemeral=True)` 内存模式；EphemeralClient 是进程级单例，init 时已做集合清理保证隔离。
- LLM：`FakeLLMClient`（conftest.py），无网络调用。

## 工作流约定

- 改进任务由 `need_improve.md` 驱动；完成后更新该文件与 `finished_improvement.md`。
- **提交前需用户明确指示**；`need_improve_tmp_dont_delete_change.md` 与 `review/` 永不提交。
- 日志统一用 `mr_data.logging.get_logger(name)`，结构化 extra 只含 `event`/`session_id`/`details`。
