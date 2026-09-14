# AG-G2-APP-007-A01 — App Runtime 原入口手工验收 Runbook

- 日期：2026-09-14
- 适用对象：测试人员 / 负责人
- 覆盖范围：仅使用原 App API 入口与既有 App Run 查询路径验证 `EVOFLOW_APP_WORKFLOW_RUNTIME` 开关关闭 / 开启、成功 / 失败 / 取消路径与回滚。
- 明确不覆盖：审批型 Runtime 人工授权流程（workflow 模式为自动授权语义，`run_app` 文档注释"点击运行即同意"，见 `app_runner.py:870`；`lead_supervised` 模式不经过 Runtime 桥接）。若未来出现真实人工审批入口，需另行派卡补充。

所有 API 路径、字段名、日志文案均回溯到现有实现：

| 依据 | 位置 |
| --- | --- |
| `POST /api/apps/{app_id}/run` | `app/gateway/routers/apps.py:485`（router prefix `/api/apps`，`apps.py:21`） |
| 请求体 `RunAppRequest` | `apps.py:87-98`：`parameters: dict[str,str]`、`execution_mode`、`run_kind`、`trigger_kind` |
| 鉴权 | router 级 `Depends(require_premium)`（`apps.py:24`）+ `require_app_visible`（`apps.py:496`，实现在 `evoflow/authz/http_guard.py:145`：App 存在且对 principal 可见，管理员直接放行） |
| `GET /api/apps/{app_id}/runs` | `apps.py:563` |
| `GET /api/apps/runs/{run_id}` | `apps.py:592` → `get_run_status`（`app_runner.py:949`），响应含 `status / progress / result_summary / error / completed_at / task_status / steps`（`app_runner.py:1146-1166`） |
| `POST /api/apps/runs/{run_id}/cancel` | `apps.py:601` |
| 桥接日志 | `app/gateway/app_runtime_bridge.py`：成功 `"app runtime bridge: run finished app_run_id=… runtime_run_id=… runtime_status=… app_run_status=…"`；失败 `"app runtime bridge: runtime execution failed app_run_id=…"` |
| 幂等键 | `app-run:{app_run_id}`（`app_runtime_bridge.app_run_idempotency_key`），Runtime repository 按 `(org_id, idempotency_key)` 复用并受唯一约束 `uq_qagent_runs_org_idempotency_key` 保护 |
| 回滚 | 开关关闭即回到 legacy 路径（`app_runner.run_app` 中 bridge 返回 False 分支），无 schema/数据回滚 |

下文 `$BASE` 为 Gateway 根地址（如 `http://127.0.0.1:8080`），`$AUTH` 为具备该 App 可见权限账号的有效认证凭据（与日常访问 App 功能相同的登录凭据）。

---

## 步骤 1：前置准备（开关关闭基线确认）

1. 选一个 `execution_mode=workflow` 的 App，记下 `app_id`（可从 App 列表页或 `GET /api/apps` 获得）。
2. 确认进程环境中**未设置** `EVOFLOW_APP_WORKFLOW_RUNTIME`（默认关闭），如有设置则移除并重启 Gateway。
3. 准备证据目录，后续每个响应与日志片段都存入其中：
   ```bash
   mkdir -p acceptance-evidence && export EVID=acceptance-evidence
   ```

## 步骤 2：开关关闭 — 旧行为 + 零 Runtime Run 可观察断言

```bash
curl -s -X POST "$BASE/api/apps/$APP_ID/run" -H "$AUTH" \
  -H 'Content-Type: application/json' \
  -d '{"parameters": {"<该App必填参数>": "smoke"}, "run_kind": "debug", "trigger_kind": "api"}' \
  | tee $EVID/off-run.json
```

- 从响应记录 `run_id`（既有响应字段，`run_app` 返回值）。
- 轮询既有查询路径直至终态（间隔 5s，与旧 workflow 行为一致）：
  ```bash
  curl -s "$BASE/api/apps/runs/$RUN_ID" -H "$AUTH" | tee $EVID/off-status.json
  ```
- 断言 A（旧行为）：`status` 终态（completed/failed）、`progress`、`result_summary` 与开关关闭时的历史行为一致。
- 断言 B（零 Runtime Run）：Gateway 日志中 grep `app runtime bridge` **零命中**——桥接代码在开关关闭时不可达（`app_runtime_bridge_enabled()` 默认 False），日志是本 Runbook 唯一新增的观察面（读取既有日志，不新增日志）。
  ```bash
  grep -c "app runtime bridge" <gateway日志>   # 期望 0
  ```

## 步骤 3：开启开关并重启

设置 `EVOFLOW_APP_WORKFLOW_RUNTIME=1`（接受 `1/true/yes/on`，见 `_TRUTHY`），重启 Gateway 使环境变量生效。开关是服务端环境变量，任何客户端输入都无法影响它。

## 步骤 4：开启 — 成功路径 + 结果可读断言

```bash
curl -s -X POST "$BASE/api/apps/$APP_ID/run" -H "$AUTH" \
  -H 'Content-Type: application/json' \
  -d '{"parameters": {"<该App必填参数>": "smoke"}, "run_kind": "debug", "trigger_kind": "api"}' \
  | tee $EVID/on-run.json
```

- 记录 `run_id` 与 `task_id`。
- 轮询 `GET /api/apps/runs/$RUN_ID` 直至终态，断言：
  - `status == "completed"`；
  - `progress == 100`；
  - `result_summary` 非空；
  - `completed_at` 非空；
  - `task_status` 仅作参考（Runtime 桥接路径下旧任务不分发，业务状态以 `status` 为准）。
- 日志断言：出现一条 `"app runtime bridge: run finished app_run_id=$RUN_ID … runtime_status=completed app_run_status=completed"`，记录其中的 `runtime_run_id`（步骤 5 幂等断言要用）。

## 步骤 5：开启 — 同 App Run 重试幂等断言

原入口每次 `POST …/run` 都生成**新的** `run_id`（新 App Run），因此"同 App Run 重试"只发生在桥接触发层对**同一 run 记录**重入时。验收断言（日志佐证级）：

- 对同一 `app_run_id` 的任何一次桥接触发，日志中该 `app_run_id` 对应的 `runtime_run_id` 必须始终相同（幂等键 `app-run:$RUN_ID` 命中 Runtime 侧既有 run，不创建第二个）。
- 如操作员具备 Runtime 库只读权限，可加一条只读佐证（非必需、不写不查业务库）：`qagent_runs` 表中 `idempotency_key='app-run:$RUN_ID'` 至多一行。
- repository 层复用与跨 org 隔离已由自动化测试证明：`test_app_runtime_bridge.py::test_same_org_retry_reuses_runtime_run_in_real_repository`、`test_cross_org_same_app_run_id_is_isolated_in_real_repository`，以及 `test_qagent_runtime.py::test_idempotency_keys_are_scoped_by_organization`。

## 步骤 6：开启 — 失败路径断言

人为制造 Runtime 执行失败（例如把 App 依赖的模型 provider 配置改为不可用值），重复步骤 4 的 POST，断言：

- 轮询后 `status == "failed"`、`error` 非空、`completed_at` 非空（`timed_out` 同样投影为 `failed`，这是冻结语义，不出现新枚举）；
- 日志出现 `"app runtime bridge: runtime execution failed app_run_id=$RUN_ID"`；
- App Run **不会**静默回落 legacy 链路重跑（日志中无旧任务分发记录）。

## 步骤 7：开启 — 取消路径断言

```bash
curl -s -X POST "$BASE/api/apps/$APP_ID/run" -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"parameters": {"<该App必填参数>": "long"}, "run_kind": "debug", "trigger_kind": "api"}'
# 取到 run_id 后立即：
curl -s -X POST "$BASE/api/apps/runs/$RUN_ID/cancel" -H "$AUTH"
```

- 连续多次 `GET /api/apps/runs/$RUN_ID`：`status` 保持 `cancelled`，不被后续轮询覆盖（终态防回退守卫 `_sync_run_from_task`，`app_runner.py:517`）。
- 如实记录（已知边界）：该 cancel 走旧 Task Center 取消路径，停止的是旧任务；Runtime 侧已 detached 启动的执行不受此调用停止。此边界已列入 AG-G2-APP-008 发布门槛清单的未覆盖项。

## 步骤 8：回滚（仅关开关）

1. 移除 `EVOFLOW_APP_WORKFLOW_RUNTIME`（或置非真值），重启 Gateway。
2. 重复步骤 2 的 POST + 轮询：新 App Run 回到旧 workflow 路径（日志 grep `app runtime bridge` 零命中、行为与基线一致）。
3. 回滚动作**仅此一项**：不涉及数据库回滚、schema 回滚或删除任何数据。已产生的 App Run 记录与日志全部保留作为证据。

## 失败时的证据收集方法（不新增日志 / API / 表）

- 每步的完整 HTTP 响应（`tee` 保存）；
- Gateway 日志中 `app runtime bridge` 相关行（含时间戳、`app_run_id`、`runtime_run_id`）；
- 对应 App Run 的最终 `GET /api/apps/runs/{run_id}` 响应全文；
- 复现所用 App 的 `app_id`、版本、参数集与开关环境变量值。

## 回溯索引

| Runbook 断言 | 既有实现 / 测试 |
| --- | --- |
| 开关默认关、零 Runtime 写 | `app_runtime_bridge_enabled()`；`test_bridge_disabled_by_default`、`test_switch_off_creates_no_runtime_run_and_leaves_run_untouched` |
| 成功投影字段 | `project_runtime_result_to_app_run`；`test_completed_result_readable_via_existing_query_path` |
| failed/timed_out→failed + error 非空 | `test_failed_result_readable_with_error_via_existing_query_path`、`test_timed_out_maps_to_failed_with_nonempty_error` |
| cancelled 防覆盖 | `test_cancelled_survives_late_task_center_polling`、`test_sync_run_from_task_does_not_regress_terminal` |
| 幂等重试 / org 隔离 | `test_same_org_retry_reuses_runtime_run_in_real_repository`、`test_cross_org_same_app_run_id_is_isolated_in_real_repository`、`test_idempotency_keys_are_scoped_by_organization` |
| 失败不回落 legacy | `test_runtime_call_failure_lands_on_failed_terminal_never_legacy[*]` |
