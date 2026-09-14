# AG-G2-APP-008-A01 — App Runtime 接入发布门槛清单

- 日期：2026-09-14
- 范围声明：本文只汇总 A01–A07 已验证事实、尚存缺口与可执行 go/no-go 条件。不是新架构设计，不是新 Plan，不含实现任务。
- 依据产物：
  - `AG-G2-APP-001-A01/20260914-a01/inventory.md`（入口与写路径盘点）
  - `AG-G2-APP-002-A01/20260914-a01/app-run-runtime-mapping.md`（生命周期映射）
  - 实现提交 `12fdeb9`（`feat(app): bridge workflow runs to runtime`）
  - `AG-G2-APP-004-A01/20260914/app-runtime-closure-review.md`（独立闭环审查）
  - `AG-G2-APP-005-A01/20260914/app-runtime-user-observability.md`（结果可读性）
  - `AG-G2-APP-006-A01/20260914/app-runtime-failure-monotonicity.md`（失败路径与单调性）
  - `AG-G2-APP-007-A01/20260914/app-runtime-manual-acceptance.md`（手工 Runbook）

---

## 1. 已验证通过项

| 项 | 证据 |
| --- | --- |
| 用户入口：`POST /api/apps/{app_id}/run`，响应契约不变，鉴权边界（`require_premium` + `require_app_visible`）不变 | A04 §4、§8；`apps.py:485-521` |
| 开关 `EVOFLOW_APP_WORKFLOW_RUNTIME` 默认关闭，关闭时零 Runtime 读写的旧行为 | A04 §1；`test_bridge_disabled_by_default` 等 |
| 幂等：`app-run:{app_run_id}`，Runtime repository 按 `(org_id, idempotency_key)` 复用 + DB 唯一约束 `uq_qagent_runs_org_idempotency_key` | A04 §2；A06 真实 repository 测试 ×2 |
| org / app 隔离：调用链逐处显式传 `org_id`，无 default-org 回退 | A04 §3 |
| 状态投影：completed/failed/timed_out→failed/cancelled 只经既有 `update_run_status` 写入 `evoflow_app_runs`；waiting_approval 永不投影 | A04 §5；A05 真实库读回测试 |
| 失败路径：create/start/grant/get_result 四个边界失败均落既有 failed 终态，不回落 legacy、不卡死 | A06 参数化 ×4 |
| 终态单调性：重复投影无副作用；Task Center 晚到轮询不回退终态 | A06 §5-9；A05 cancel 存活测试 |
| 旧状态源唯一性：用户可见状态仅来自 `evoflow_app_runs`（`load_run` / `GET /api/apps/runs/{run_id}`） | A05 全部读回测试 |
| 回滚：关开关即回 legacy 路径，无 schema/数据回滚 | A04 §1；Runbook 步骤 8 |

## 2. 未覆盖项与分级

分级定义：
- **[阻断测试环境开启]** — 不满足则测试环境不得开启开关；
- **[阻断生产]** — 不阻断测试环境灰度，但生产默认开启前必须解决；
- **[观察项]** — 记录在案，暂不阻止开启。

| # | 未覆盖项 | 分级 | 说明 |
| --- | --- | --- | --- |
| U1 | Runtime 依赖 Postgres（`RuntimeRepository.from_config` 要求 `get_postgres_url(required=True)`）。开关开启但 Postgres 未配置时，每次 run 都会走失败终态路径 | **[阻断测试环境开启]** | 测试环境开启前必须先配好 Runtime 的 Postgres 连接；否则用户看到的全是 failed |
| U2 | 测试环境必须先按 Runbook（AG-G2-APP-007）完成一轮真实执行（成功 + 失败 + 取消） | **[阻断测试环境开启]** | 自动化测试全部使用 fake / 内存库，未覆盖真实 LLM 与真实外部依赖 |
| U3 | 真实生产并发：多实例 / 多进程下同一 run 的触发竞争、detached poll 调度器跨进程行为 | **[阻断生产]** | DB 唯一约束做了最坏情况兜底（不会双 run），但调度与投影的并发语义未验证 |
| U4 | 人工审批流：自动 `grant_approval` 无 `decided_by` 记录；无真实人工审批入口可验证 | **[阻断生产]** | workflow 模式"点击运行即同意"语义一致（A04 §4），但审计痕迹缺失 |
| U5 | 取消语义：旧 cancel 只停旧 Task Center 任务，不停 Runtime 侧已 detached 启动的执行 | **[阻断生产]** | 终态守卫保证业务状态不被覆盖，但资源消耗与"取消即停"的用户预期不符 |
| U6 | 长时间任务：`get_result` 在 detached 协程内的等待上限、进程重启后的 run 恢复 | **[阻断生产]** | 短 smoke 任务未暴露；进程重启后 in-flight Runtime run 的投影归属未定义 |
| U7 | 多实例部署、高可用、灾备、数据迁移、压测 | **[阻断生产]** | 完全未做；本清单明确声明这些**不存在已完成状态** |
| U8 | A03 验收产物文档未入库（仓库只有实现提交 `12fdeb9`，无 AG-G2-APP-003 对应 artifacts 目录） | **[观察项]** | 交接缺口，建议补交归档；映射依据可读 `AG-G2-APP-002` 产物与模块 docstring |
| U9 | failed/cancelled/timed_out 终态 `result_summary` 为空（信息在 `error` 字段） | **[观察项]** | 冻结映射语义，非缺陷 |
| U10 | G0 未冻结契约 | **[观察项]** | 本清单不把任何未冻结的 G0 契约标为已解决；相关契约仍以 runtime roadmap 冻结记录为准 |

## 3. 结论

- **TEST GO（测试环境可灰度开启）** — 前提：U1（Postgres 就绪）与 U2（按 Runbook 走完真实一轮）满足；建议单实例灰度（U3 未验证前避免多实例）；对 U5 取消边界知悉并接受观察。
- **TEST NO-GO 触发条件** — Postgres 未配置、定向测试出现新失败、Runbook 步骤 2 断言 B（零 Runtime Run）不成立。
- **PROD NO-GO（生产默认开启：否）** — U3–U7 全部未解决。生产如需提前体验，只能按运行白名单方式个案评估，不得默认开启。

## 4. 最小回滚动作

关闭 `EVOFLOW_APP_WORKFLOW_RUNTIME` 并重启 Gateway；验证一次新 App Run 回到旧 workflow 路径（Runbook 步骤 8：日志 `app runtime bridge` 零命中 + 行为与基线一致）。无数据库回滚、无 schema 回滚、无删数据。

## 5. 定向测试清单与最近一次通过结果（2026-09-14，HEAD `30bea77`）

| 测试 | 结果 |
| --- | --- |
| `app/tests/test_app_runtime_bridge.py` | 31 passed |
| `packages/harness/tests/test_workflow_run_dispatch.py` | 与上文件合跑共 34 passed（A06）；A05 起持续通过 |
| `uv run --frozen ruff check app/tests/test_app_runtime_bridge.py` | All checks passed |

## 6. 存量失败（test_app_revisions.py）

`packages/harness/tests/test_app_revisions.py::test_save_app_writes_revision` 与 `::test_restore_app_from_revision` 两个失败已按 AG-G2-APP-004-A01 §7 在 `12fdeb9^`（parent `5f5571c`）与 `12fdeb9`（current）两个隔离临时 worktree 独立运行验证：**两处同形失败**（`ValueError: Revision not found`），为存量问题，与 Runtime bridge 无因果，不阻断本任务包测试/文档/Runbook 卡；修复需另行派卡。
