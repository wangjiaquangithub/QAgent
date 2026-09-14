# AG-G2-APP-004-A01 — App Runtime Closure 独立审查

- 审查对象：`12fdeb967964b2068fcd93e8d2288345ce6b3a49`（`feat(app): bridge workflow runs to runtime`）
- 审查方式：只读代码审查 + 隔离临时 worktree 实测回归；未修改任何生产逻辑。
- 审查日期：2026-09-14
- 审查工作区：`/Users/wangjiaquan/project/QAgent-agentscope-runtime`，分支 `codex/agentscope-runtime`，HEAD = origin/codex/agentscope-runtime = `12fdeb9`

生产改动面（4 文件）：

| 文件 | 改动 | 职责 |
| --- | --- | --- |
| `backend/app/gateway/app_runtime_bridge.py` | +337（新增） | 桥接模块：开关判断、幂等键派生、Runtime 调用序列、终态投影、失败终态写入 |
| `backend/app/gateway/routers/apps.py` | +7 | HTTP 入口解析认证 org_id 并传入 `run_app`，响应契约不变 |
| `backend/packages/harness/evoflow/collab/app_runner.py` | +31/-1 | `run_app` 增加 org_id 参数与桥接触发点；`_sync_run_from_task` 增加终态防回退守卫 |
| `backend/app/tests/test_app_runtime_bridge.py` | +480（新增） | 桥接契约测试（19 条测试的一部分） |

---

## 逐条契约审查

### 1. 开关默认关闭时旧行为保持、Runtime 侧零写入 — ✅ 成立（风险：低）

- 证据：`app_runtime_bridge.py::app_runtime_bridge_enabled()`（`os.getenv(_ENV_SWITCH, "").strip().lower() in _TRUTHY`，默认空串 → False）。
- 唯一触发点：`app_runner.py::run_app` 中 `trigger_workflow_app_runtime_run(...)`，其第一行即 `if not app_runtime_bridge_enabled() or not str(org_id or "").strip(): return False`，返回 False 后走原 legacy 分发，路径与旧代码逐字节等价（diff 中 bridge 之后的 legacy 代码未动）。
- 开关关闭时 `run_app_workflow_on_runtime` 不会被调用，Runtime 侧零读零写。
- 测试：`test_bridge_disabled_by_default`、`test_trigger_without_opt_in_reports_not_applicable`（`app/tests/test_app_runtime_bridge.py`）。
- 结论：**通过**，无需后续修复。

### 2. 同一 App Run 重试必须复用同一 Runtime Run — ✅ 成立（风险：低）

- 幂等键：`app_run_idempotency_key()` → `app-run:{app_run_id}`，由既有稳定 App Run id 派生。
- Runtime 侧：`app/qagent_runtime/repository.py::create_run` 先按 `(org_id, idempotency_key)` 查既有行，命中即返回既有 run（不创建第二个）。
- DB 兜底：`app/qagent_runtime/models.py:36` 存在唯一约束 `uq_qagent_runs_org_idempotency_key`（org_id + idempotency_key），并发重试也不会产生第二行。
- 既有测试：`app/tests/test_qagent_runtime.py::test_idempotency_keys_are_scoped_by_organization`（Runtime repository 层）；桥接层 `test_idempotency_key_derived_from_app_run_id`。
- 结论：**通过**，无需后续修复。

### 3. 不同 org_id / app_run_id / app_id 不串用 — ✅ 成立（风险：低）

- 幂等查找与唯一约束均含 `org_id`：不同 org 同文本 app_run_id → 不同 Runtime Run（`test_idempotency_keys_are_scoped_by_organization` 直接覆盖）。
- 不同 app_run_id → 幂等键不同 → 必然不同 Runtime Run。`evoflow_app_runs.id` 全局唯一（`_id()` 生成），不同 app_id 的 run 不可能同 id，故"同 key 不同 app"场景不存在。
- `create_run` / `start_run` / `get_run_status` / `grant_approval` / `get_result` 全部显式携带 `org_id`（`run_app_workflow_on_runtime` 内逐处传参），无 default-org 回退（入口 `apps.py` 无 org 时传 None → 桥接直接 return False 走 legacy）。
- 结论：**通过**，无需后续修复。

### 4. waiting_approval 自动 grant_approval 的权限边界 — ✅ 成立，附一条观察项（风险：中低）

- 权限校验发生的层：**HTTP 入口层**。`backend/app/gateway/routers/apps.py::run_app_endpoint` 在调用 `run_app` 之前执行 `require_app_visible(http_request, app_id)`（`evoflow/authz/http_guard.py`），并在 12fdeb9 中新增解析 `resolve_request_principal(http_request).get("org_id")` 传入。即：只有通过既有认证 + app 可见性校验的真实用户入口发起的 run，才可能进入桥接。
- 自动授权是**服务内部调用**：`run_app_workflow_on_runtime` 直接调用 `RuntimeService.grant_approval`（`app/qagent_runtime/service.py:178`），不经过任何 HTTP Runtime 路由，无法被外部请求伪造；`grant_approval` 内部经 `decide_approval_atomically`（org_id + run_id 双重过滤）且持有 per-run 锁。org 隔离成立。
- 语义依据："点击运行即同意"（workflow 模式本来就自动授权 legacy 链路），自动 grant 与既有 workflow 模式语义一致，**未绕过用户授权边界**。
- 观察项（非阻断）：`grant_approval` 未传 `decided_by`，审批记录中没有留下"代表哪个用户/主体授权"的痕迹。建议后续卡（不属本任务包）在桥接中补传 principal。当前不影响契约成立。
- 测试：`test_waiting_approval_is_granted_then_result_projected`。
- 结论：**通过**；观察项记录在案，不需要本任务包修复。

### 5. completed / failed / timed_out / cancelled 投影与既有 evoflow_app_runs 一致 — ✅ 成立（风险：低）

- 投影只经既有写点 `evoflow/persistence/app_repositories.py::update_run_status`（该函数白名单字段：progress / result_summary / error / started_at / completed_at；终态自动落 completed_at）。无新字段、无新表、无第二状态源。
- 映射：completed→completed(progress=100, result_summary)；failed→failed(error)；timed_out→failed(error="Runtime run timed out"/payload)；cancelled→cancelled(error)。`waiting_approval` 永不投影（非 `_TERMINAL_RUNTIME_STATUSES` 成员）。
- 与冻结的 Automation 域 timed_out→failed 语义一致（模块 docstring 引 AG-G2-APP-002-A01 mapping）。
- 测试：`test_completed_projection`、`test_failed_projection_writes_existing_error_field`、`test_timed_out_maps_to_existing_failed_enum`、`test_cancelled_projection`。
- 结论：**通过**，无需后续修复。

### 6. `_sync_run_from_task` 终态防回退守卫不会吞掉合法的旧 Task Center 更新 — ✅ 成立（风险：低）

- 守卫逻辑（`app_runner.py:517-524`）：App Run 已是 completed/failed/cancelled 时直接 `return current`，仅对 completed 且 progress<100 补齐 100。
- 合法更新不受影响的论证：
  - legacy 路径下终态本来就由任务轮询自己写入（同一写点），写入后 run 已 terminal，后续轮询本无新状态可写；
  - 用户取消（cancelled）后任务晚到 completed：守卫保持 cancelled。旧代码会把 completed 覆盖回 cancelled——行为变化存在，但这正是 AG-G2-AUTO-011 冻结的单调性语义（用户取消优先于晚到的任务终态），属有意修正而非吞更新；
  - paused / executing / planned 等非终态全部照旧走 `mapped` 更新分支，进度刷新照旧。
- 测试：`test_sync_run_from_task_does_not_regress_terminal`、`test_sync_run_from_task_still_fixes_completed_progress`。
- 结论：**通过**，无需后续修复。

### 7. test_app_revisions.py 两个失败的 parent/current 独立对比 — ✅ 存量问题，实证成立

方法：干净创建两个 detached 临时 worktree（`git worktree add --detach`），只读运行测试，正常 `git worktree remove`（未用 --force），未触碰集成 worktree。

| 提交 | 位置 | 结果 |
| --- | --- | --- |
| `12fdeb9^`（= `5f5571c`，parent） | `/tmp/qag-g2app-parent` | `2 failed, 1 passed` — `test_save_app_writes_revision`、`test_restore_app_from_revision` |
| `12fdeb9`（current） | `/tmp/qag-g2app-current` | `2 failed, 1 passed` — 同样两个失败，同样错误 `ValueError: Revision not found: App_restore1@v1`（`app_repositories.py:163`） |

结论：两个失败在 parent 与 current **完全同形**，为存量问题，与 12fdeb9 无因果关系。按任务包规则记录为存量问题，不阻断后续纯测试/文档/Runbook 卡。
（注：该文件实际路径为 `packages/harness/tests/test_app_revisions.py`，不在 `app/tests/` 下；任务包文本中的 `app/tests/` 路径系笔误。）

### 8. A03 生产改动规模与功能目标匹配度 — ✅ 匹配（风险：低）

| 文件 | 职责 | 风险 | 最小回归面 |
| --- | --- | --- | --- |
| `app_runtime_bridge.py`（新增，337 行） | 全部桥接逻辑；默认惰性（开关关 → 不可达） | 低：纯新增模块，无既有代码依赖它（仅 `run_app` 一处 import，且在 `if org_id:` 分支内） | 开关开启时的 App Workflow run |
| `routers/apps.py`（+7） | 解析 org_id 传参；响应不变 | 低：仅在 `run_app_endpoint` 内增加读 principal 与一个传参 | `POST /api/apps/{app_id}/run` 响应契约（无变化，`test_workflow_run_dispatch.py` 覆盖） |
| `app_runner.py`（+31/-1） | ① `run_app` 增加 org_id 形参与桥接触发分支（org_id 为 None 时零行为差异）；② `_sync_run_from_task` 终态守卫 | 中低：守卫改变了"晚到任务终态覆盖 App Run 终态"的旧行为（见第 6 条论证） | 所有 App Run 的任务轮询同步路径 |
| `test_app_runtime_bridge.py`（新增，480 行） | 桥接契约测试 | 无生产风险 | — |

改动未触及 qagent_runtime、migrations、Gateway 主干、LangGraph 主链、依赖与锁文件；规模（约 375 行生产代码）与"一条入口 → 一次 Runtime 闭环"的功能目标匹配，无明显过度设计。

---

## 回归验证原始结果

```
cd backend
uv run --frozen python -m pytest -q app/tests/test_app_runtime_bridge.py packages/harness/tests/test_workflow_run_dispatch.py
→ 19 passed in 4.80s
```

（注：任务包所写 `app/tests/test_workflow_run_dispatch.py` 实际位于 `packages/harness/tests/`，按实际路径执行。）

未改动测试文件，故 ruff 检查不适用（无改动文件可查）。

## 汇总

| # | 契约 | 结论 | 风险 | 需后续修复 |
| --- | --- | --- | --- | --- |
| 1 | 默认关闭零写入 | ✅ | 低 | 否 |
| 2 | 重试复用同一 Runtime Run | ✅ | 低 | 否 |
| 3 | org/app 隔离 | ✅ | 低 | 否 |
| 4 | 自动授权权限边界 | ✅（1 观察项：decided_by 未记录） | 中低 | 否（记为后续卡建议） |
| 5 | 终态投影一致 | ✅ | 低 | 否 |
| 6 | 守卫不吞合法更新 | ✅ | 低 | 否 |
| 7 | test_app_revisions 存量对比 | ✅ 存量问题实证 | — | 否（另立卡处理） |
| 8 | 改动规模匹配 | ✅ | 低 | 否 |

无阻断问题，可进入 AG-G2-APP-005-A01。
