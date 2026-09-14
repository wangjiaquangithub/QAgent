# AG-G0-DB-004-A01 — Migration revision / 顺序 / 可回滚边界地图

- 日期：2026-09-14
- 分支：`codex/agentscope-runtime`，HEAD `f290af9`
- 母任务：G0-DB-004（migration 升级、失败和回滚验证）
- 性质：只读盘点。唯一验收命令（原样执行）：

```
rg -n -e 'revision' -e 'down_revision' -e 'def upgrade' -e 'def downgrade' backend/migrations/versions
```

## 1. Revision 链（单链、单 head，无分支）

| 顺序 | revision | down_revision | upgrade 内容 | downgrade 内容 | 可回滚边界 |
| --- | --- | --- | --- | --- | --- |
| 1 | `0001_postgres_foundation` | None | 创建 foundation 标记表（`2dfedec` 引入） | `op.drop_table` 标记表 | 可回滚；无业务数据 |
| 2 | `0002_qagent_runtime` | `0001_postgres_foundation` | 创建 Runtime 五类核心表（`qagent_runs`、`qagent_run_events`、`qagent_approvals`、`qagent_recovery_points`、`qagent_run_assets`）、索引与 Run sequence 唯一约束 | 按 FK 安全顺序 drop 索引 + drop 表 | 可回滚但**数据即弃**：回滚 = 全部 Runtime 数据删除（这是设计语义，不是缺陷） |
| 3 | `0003_qagent_runtime_org_scoping` | `0002_qagent_runtime` | `qagent_runs` / `qagent_approvals` 增加 `org_id` 列 + 索引 | drop 两列与索引 | 可回滚但**丢 org 归属**：回滚后无法再区分 org |
| 4 | `0004_qagent_runtime_org_idempotency` | `0003_qagent_runtime_org_scoping` | 以 `(org_id, idempotency_key)` 唯一约束 `uq_qagent_runs_org_idempotency_key` 取代旧全局约束 | drop 新约束、重建旧全局 `uq_qagent_runs_idempotency_key` | **条件不可逆**：若数据中同一 `idempotency_key` 已被多个 org 使用，重建全局唯一约束会因唯一冲突直接失败。回滚前必须先做数据对账（见 §3） |
| 5 | `0005_qagent_runtime_execution_claim` | `0004_qagent_runtime_org_idempotency` | `qagent_runs` 增加 `execution_claim` / `execution_epoch`（server_default 0）/ `execution_claimed_at` | 逆序 drop 三列 | 可回滚；丢失的 claim 状态恢复后由 resume/reclaim 语义重建 |

## 2. 配置与入口事实

- `backend/alembic.ini`：`script_location = %(here)s/migrations`，`sqlalchemy.url` 留空。
- `backend/migrations/env.py`：URL 一律经 `app.postgres.config.get_postgres_url()` 解析（`QAGENT_POSTGRES_URL` 优先，其次 `QAGENT_POSTGRES_*` 分变量）；**不使用** `QAGENT_POSTGRES_VERIFY_URL`（该 URL 专用于 `qagent_foundation_test` 破坏性验证）。
- `env.py` 含 `version_table` 宽度自愈逻辑（`_ensure_version_table_width`），升级入口对旧版本表兼容。
- head = `0005_qagent_runtime_execution_claim`；升级命令形态：`cd backend && alembic upgrade head`（真实 PG），或经 `scripts/postgres_verify.py`（自带 foundation/migration 检查，目标库固定 `qagent_foundation_test`）。

## 3. 风险登记（供 A02–A04 与负责人使用）

1. **所有 downgrade 都是数据破坏性的**（drop 表/列）。回滚 ≠ 数据保全；备份/恢复属于 `G0-DB-005`，不在 migration 层内。
2. **0004 downgrade 条件不可逆**（多 org 复用同一 idempotency key 时重建全局约束失败）。这是本链中唯一非无条件可回滚点；A03 失败注入如覆盖该点，预期结果应为"回滚报唯一冲突、迁移版本保持不变、可重试"。
3. `0005` 的 `execution_epoch` 带 `server_default "0"`（NOT NULL），对存量行安全；downgrade drop 列在 PG 上是元数据操作，锁表时间与表大小相关，大表回滚需在维护窗口做（记录为风险，不在本卡验证）。
4. 未发现 downgrade 缺失或空实现的 revision；未发现分支/多 head。

## 4. 结论

- 升级顺序确定性：单链 0001→0005，无分支，无多 head。
- 可回滚边界：0001/0002/0003/0005 无条件可回滚（但破坏数据）；0004 条件可回滚（§3.2）。
- 卡片停止条件未触发（未发现"downgrade 不可逆且无解释"的项；0004 的条件不可逆已按卡片要求记录）。
- 后续：A02（空库完整 upgrade 证据）需要一次性 disposable PostgreSQL 连接凭据；当前环境 `pg_hba.conf` 全部 `scram-sha-256` 且无可用凭据（同 AG-G2-APP-011-A01 的阻塞），A02–A04 待凭据后执行。
