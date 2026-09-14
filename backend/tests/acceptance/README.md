# AgentScope 2 验收测试脚手架

本目录承载 AgentScope 2 替换、PostgreSQL 正式状态和本地设备协议的**验收测试准备工作**，以及已在真实 PostgreSQL 上落地的 G0.7 Runtime 并发验收。

## 目录内容

| 文件 | 作用 |
| --- | --- |
| `test_data.py` | 只生成可复现的测试 ID 和证据目录，不创建生产数据 |
| `test_scaffold.py` | 脚手架自身的完整性检查 |
| `pg_support.py` | 真实 PostgreSQL 共享骨架（专库校验、反 SQLite 守卫、双连接竞态、证据模型、fixture） |
| `test_g07_approval_races.py` | G0.7 场景 1（Grant vs Reject）、场景 2（Cancel vs Grant） |
| `test_g07_execution_claim.py` | G0.7 场景 3（claim 抢占）、场景 4（过期回收）、场景 5（陈旧 epoch 拒绝） |
| `test_g07_event_sequence.py` | G0.7 场景 6（决策事务回滚）、场景 7（Event sequence 并发） |
| `conftest.py` | 验收 fixture；并在本目录内修正 `app` 包被 harness 遮蔽的问题 |

## 约定

- 测试以 `agentscope_acceptance` marker 标记；真实 PostgreSQL 场景另加 `postgres` marker。
- `test_data.py` 只生成可复现的测试 ID 和证据目录，不创建生产数据。
- 每个验收用例必须关联 `org_id`、`run_id`、`command_id` 或 `migration_batch_id`（按场景适用），并把日志、事件序列、数据库快照 / SQL、故障注入时间线和最终结论写入证据目录。
- 集成测试默认使用隔离 PostgreSQL、可控的 fake device transport 和可重置的资产目录；不得复用开发机正式数据库或真实企业资产。
- 需要真实进程重启、备份恢复或部署扫描的用例，归入人工演练或 CI / 发布流水线，而不是在单元测试中伪造。

## 运行 G0.7 真实 PostgreSQL 并发验收

前置：

1. 一个专用 PostgreSQL 实例（禁止使用开发 / 生产库）；
2. 专用数据库名必须严格为 `qagent_foundation_test`，且迁移已到 `head`；
3. 环境变量：

```bash
export QAGENT_RUN_POSTGRES_TESTS=1              # opt-in 门禁；不设则全部 skip
export QAGENT_POSTGRES_VERIFY_URL="postgresql+psycopg://<user>:<pw>@127.0.0.1:5432/qagent_foundation_test"
# 可选：证据输出目录，默认 backend/.tmp/g07-postgres-concurrency（已被 gitignore）
export QAGENT_G07_EVIDENCE_DIR="/path/to/evidence"
```

运行：

```bash
cd backend
python -m pytest tests/acceptance -v
```

安全设计：

- `QAGENT_POSTGRES_VERIFY_URL` **永不**回退到 `QAGENT_POSTGRES_URL`；
- 破坏性 SQL 前在**执行连接本身**上校验 `SELECT current_database()` 严格等于 `qagent_foundation_test`；
- `assert_is_postgres()` 拒绝任何非 PostgreSQL 方言，杜绝静默回退 SQLite；
- 涉及并发的场景断言两个 worker 的 `pg_backend_pid()` 不同，证明确为多连接。

详细验收结论、缺陷与证据见 [`docs/runbooks/g0.7-postgres-runtime-concurrency-acceptance.md`](../../../docs/runbooks/g0.7-postgres-runtime-concurrency-acceptance.md)。

## 其他验收场景

`agentscope-2-acceptance-test-matrix.md` 中的设备、资产、迁移、备份恢复与 LangGraph 清理场景，需等阶段 0 和最小纵向切片冻结接口后再实现。
