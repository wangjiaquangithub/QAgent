# AG-G2-AUTO-002-A01 — Automation / Task Center 对象与 Runtime 字段级映射

- 卡片：`AG-G2-AUTO-002-A01`（母任务 `G2-AUTO-002`，阶段 G2，Owner Domain Migration + Runtime）
- 动作类型：只读映射（不修改任何源代码或计划外文档）
- 上游：`AG-G2-AUTO-001-A01`（`artifacts/acceptance/agentscope-runtime/AG-G2-AUTO-001-A01/20260915/inventory.md`）
- 工作目录 / 分支：`/Users/wangjiaquan/project/QAgent-agentscope-runtime` / `codex/agentscope-runtime`
- 起始 HEAD：`fc8bbf8bb41718ac20743e4b0a740b25093d2658`
- attempt：`20260915`
- 验收命令（原文）：
  ```bash
  rg -n "TaskAuthorizedEvent|TaskExecutionStartedEvent|TaskExecutionFailedEvent|TaskCancelEvent|TaskCancelledEvent|TaskResumeEvent|task_runtime_" backend/app/gateway
  ```
  原始输出：`rg-hits.txt`（同目录，107 行，exit=0）。

> 本卡只做对照登记：**不重新定义**已冻结语义（`docs/plan/agentscope-2-g2-automation-completion-checklist.md` §1.4），**不新增** `TaskStatus` 枚举值、字段、接口或 schema。

---

## 1. 映射总表（Task Center → Runtime）

| Task Center 侧（证据文件 / 符号） | Runtime 侧（证据文件 / 符号） | 确定值 | 未知 / 未接线 | G0 依赖 | A03 是否覆盖 |
| --- | --- | --- | --- | --- | --- |
| 任务行 `id`（`evoflow_collab_tasks.id`，`unattended_task_pipeline.find_main_task`） | `task_runtime_context.build_task_runtime_context:291` → `runtime_task_id = "tc-" + sha256(org_scope_key, task_id)[:24]`；经 `create_run(task_id=...)` 传入 | 确定性派生，按组织分区 | — | — | 是（只读复用） |
| 任务行 `unattended_attempts` | `task_runtime_context._attempt_of:267` / `idempotency_key_for:271` → `idempotency_key = "tc:" + sha256(scope, task_id, attempt)[:32]` | 同 `(org scope, task, attempt)` 恒等 | 跨实例同窗口去重语义未定义 | `G0-DEC-002`（跨实例触发幂等） | 否（本卡只登记） |
| 任务行 ACL 列 `org_id` / owner scope / created_by | `task_runtime_optin.resolve_server_task_runtime_identity:269` → `create_run(org_id=...)` | 服务端解析，三者任一为空即返回 `None` → legacy | `RuntimeContract.create_run`（`qagent_runtime/contract.py:32`）未声明 `org_id`（缺口，已登记） | — | 是（只读复用） |
| 任务行 `run_mode == "unattended"` | `task_runtime_optin.is_unattended` 判定；`task_runtime_schedule._UNATTENDED_RUN_MODE:62` | 字面量 `unattended` | — | — | 是 |
| 任务行 `execution_mode` / `prompt_execution_mode` | `task_runtime_optin.classify_execution_mode:194`；opt-in `task_center / plan / unattended / task_center_plan`，opt-out `direct / direct_langgraph / langgraph / execute / runs_wait`，其余 `invalid` | 归一化：小写 + `-`→`_`；非法值不 opt-in | — | — | 否（A03 不改路由） |
| 任务行扩展槽 `runtime_run_linkage`（`task_runtime_linkage.LINKAGE_TASK_KEY:77`） | `RuntimeRunLinkage(runtime_run_id, org_scope_key, task_id, idempotency_key)`（`:94-107`） | 4 字段；`read_linked_runtime_run:205` 做跨 org / 跨任务拒绝 | — | — | 是（读取） |
| 任务行扩展槽 `runtime_run_cursor`（`task_runtime_cursor.CURSOR_TASK_KEY:55`） | `read_runtime_cursor:139` / `advance_runtime_cursor:164` | 每 Run 水位；`applied_watermark:230` | 流式订阅未接线 | `G0-DEC-003` | 否（A03 不引入 cursor 语义） |
| 任务行 `status`（`TaskStatus`，`collab/models.py:59`） | `task_runtime_projection._RUNTIME_TO_TASK_STATUS:67` | 见 §2 状态映射表 | — | — | **是（A03 唯一写点）** |
| 任务行 `execution_history`（list[dict]） | `task_runtime_projection.build_runtime_history_record:322` / `_build_record:270` | `source="qagent_runtime"`、`runtime_run_id`、`runtime_status`、`org_scope_key`、`approval_required`，可选 `event_id` / `sequence` / `error_code` / `reason` / `error_detail_redacted` / `hint` / `result_available` / `result` | — | — | **是（A03 唯一写点）** |
| 任务行 `unattended_stage`、`progress`、`error`、`completed_at` / `failed_at` | 无对应 Runtime 字段 | 由既有 pipeline 收敛逻辑（`_finalize_executing_task:457`）写入，**不由 Runtime 投影写入** | — | — | 否（A03 不写这些字段） |
| 任务行 `execution_authorized` / `authorized_at` / `authorized_by` | `task_runtime_context` 只读 `authorized` 前置；`create_run` 前必须为真 | 只读前置，不授予 | approval 授予语义 | `G0-DEC-001`（cancel 后 approval 终态） | 否 |
| Runtime Run `run_id` | `task_runtime_optin.RuntimeRunContract.get_run_status:148`；`service.get_run_status:301` | 公开读取 | — | — | 是（读取） |
| Runtime Run `status` | `qagent_runtime/events.py:29 TERMINAL_STATUSES = {completed, failed, cancelled, timed_out}`；`:30 INCOMPLETE_STATUSES` | 词汇已冻结 | `waiting_approval` 的终态处理 | `G0-DEC-001` | 部分（见 §2） |
| Runtime Run 结果 | `task_runtime_result.sanitize_result_summary`（白名单摘要） | 只投 `result_available` + 脱敏 `result` | — | — | 是（`completed` 分支） |
| Runtime Run 错误 | `task_runtime_failure.sanitize_error_code` / `sanitize_failure_detail` | `error_code` + 脱敏 `reason`；被扣留时置 `error_detail_redacted=true` | — | — | 是（`failed` / `timed_out` 分支） |
| Runtime 取消 | `task_runtime_cancel.cancel_linked_runtime_run`（`RuntimeCancelOutcome.action/reason`，`:113-117`）；`request_cancel:324` | 已接线（`routers/tasks.py:1919`） | cancel 与 approval 竞态 | `G0-DEC-001` | 否（已完成，不重派） |
| Runtime 事件帧 | `task_runtime_event_bridge.runtime_status_for_event_type:94` / `apply_runtime_event:128`；帧形状 `qagent_runtime.events.event_dict:37` | 帧字段 `event_id` / `run_id` / `sequence` / `occurred_at` / `type` / `payload` | 流式订阅 / cursor / gap | `G0-DEC-003` | 否 |
| Runtime 恢复点 / attempt | `qagent_runtime` Recovery Point；`task_runtime_recovery.recover_task_stream:200`、`task_runtime_reconcile.reconcile_linked_task_runtime:102` | 模块已实现 | 重启后自动再投影未接线 | `G0-DEC-004` | 否 |
| 坏链路 fail-safe 展示 | `task_runtime_degrade.assess_runtime_link:173` / `reconcile_task_runtime_safely:264` | 模块已实现 | 未接线 | — | 否 |

---

## 2. 状态映射（沿用 `completion-checklist.md` §1.4 已冻结语义，本卡不重新定义）

`task_runtime_projection._RUNTIME_TO_TASK_STATUS:67` 与冻结表逐项一致：

| Runtime 状态 | Task 状态（既有 `TaskStatus` 值） | `execution_history` 记录 | 备注 |
| --- | --- | --- | --- |
| `created` | `pending` | 追加记录 | — |
| `queued` | `pending` | 追加记录 | — |
| `planning` | `planning` | 追加记录 | — |
| `waiting_approval` | **不变**（既不 `paused` 也不 `planned`） | 追加 `approval_required=true` + 固定 hint | 受 `G0-DEC-001` 影响，只登记 |
| `running` | `executing` | 追加记录 | — |
| `executing` | `executing` | 追加记录 | — |
| `completed` | `completed` | 追加记录 + `result_available`（+ 脱敏 `result`） | 终态 |
| `failed` | `failed` | 追加记录 + 脱敏 `error_code` / `reason` | 终态 |
| `cancelled` | `cancelled` | 追加记录 | 终态 |
| `timed_out` | `failed` | 保留 `runtime_status=timed_out` + 脱敏 `error_code` / `reason` | 终态 |
| `approval_granted` / `approval_rejected` | **不变**（marker，无 stage rank） | 追加 marker 记录 + 固定 hint | 受 `G0-DEC-001` 影响，只登记 |

**未新增** `TaskStatus` 枚举值。现有集合：`inbox / pending / planning / planned / executing / paused / reviewed / completed / failed / cancelled`。

### 2.1 单调性规则（`task_runtime_projection.py:24-37`，已实现）

- 每 Run 独立 stage rank（`_RUNTIME_STAGE_RANK:103`：`created=0, queued=1, planning=2, waiting_approval=3, running=4, executing=5`）；
- 终态任务不被后续终态覆盖（`_TERMINAL_TASK_STATUSES` 分支 `:426-430`）；
- 同一 Run 内落后 stage 被拒（`:470-480`，`stale/runtime_status_regression`）；
- 已记录的取消使该 Run 收敛到 `cancelled`，之后任何帧都不再移动任务（`:437-457`）；
- 重复 `event_id` / `sequence` / 同 stage 均为 `noop`（`_is_duplicate:170`、`:478-480`）。

---

## 3. Task Center 事件 ↔ Runtime 事件对照

Task Center 事件为**进程内 dataclass**（`backend/app/gateway/events/task_events.py`，非持久化契约）；Runtime 事件为 **Event v1 持久化契约**（`qagent_runtime/events.py:14 EventType`）。两者**不是同一套词汇**，本卡只做对照，不引入桥接。

| Task Center 事件（`task_events.py`） | 字段 | Runtime Event v1 对应 | 对照结论 |
| --- | --- | --- | --- |
| `TaskAuthorizedEvent:12` | `task_id`、`project_id`、`authorized_by`、`thread_id`、`timestamp`、`event_id`、`source` | 无直接对应；语义最接近 `approval.granted`（`EventType.APPROVAL_GRANTED`） | **不一一对应**：TC 侧是「任务被授权执行」的进程内信号，Runtime 侧 `approval.granted` 是 Run 级审批结果；且 TC 侧 `authorized_by` 无 Runtime 对应字段 |
| `TaskExecutionStartedEvent:47` | `task_id`、`project_id`、`started_at`、`subtask_count`、`event_id`、`triggered_by` | 语义对应 `run.executing`（`EventType.RUN_EXECUTING`） | **不一一对应**：TC 侧描述子任务委派开始，Runtime 侧描述 Run 进入 executing；`subtask_count` 在 Runtime 无对应字段 |
| `TaskExecutionFailedEvent:59` | `task_id`、`project_id`、`error`、`failed_at`、`retryable`、`event_id`、`attempt_count` | 语义对应 `run.failed`（`EventType.RUN_FAILED`） | **不一一对应**：`retryable` / `attempt_count` 在 Runtime Event v1 无对应字段；重试语义受 `G0-DEC-004` 约束 |
| `TaskCancelEvent:72` | `task_id`、`project_id`、`subtask_ids`、`cancelled_by`、`reason`、`timestamp`、`event_id` | 语义对应 `run.cancelled`（`EventType.RUN_CANCELLED`） | **不一一对应**：TC 侧是「取消请求」，Runtime 侧是「已取消」；`subtask_ids` / `cancelled_by` 在 Runtime 无对应字段 |
| `TaskCancelledEvent:104` | `task_id`、`project_id`、`cancelled_at`、`cancelled_by`、`subtask_count`、`event_id` | 语义对应 `run.cancelled` | **不一一对应**：同 Run 只能有一个 `run.cancelled`；TC 侧 `subtask_count` 无对应 |
| `TaskResumeEvent:116` | `task_id`、`project_id`、`resumed_by`、`timestamp`、`event_id`、`source` | 无直接对应；Runtime 侧对应语义在 `resume_run:337` + `G0-DEC-004` 的 attempt 语义 | **不一一对应**：Runtime 的 resume 是「新 attempt」，TC 侧 resume 是「暂停后继续」；`resumed_by` 无对应字段 |

**结论**：Task Center 事件与 Runtime Event v1 **不能直接互换**，也不在本卡范围内建立桥接。`task_runtime_event_bridge.apply_runtime_event:128` 只做 Runtime → Task history 的**单向状态投影**，不生成 TC 事件。

---

## 4. 取消路径对照

| 环节 | 证据 | 状态 |
| --- | --- | --- |
| 真实入口 | `POST /api/tasks/{task_id}/cancel`（`routers/tasks.py:1971`） | 已接线 |
| 转发点 | `routers/tasks.py:1919 _cancel_linked_runtime_run_if_any` → `:1947 cancel_linked_runtime_run` | 已接线 |
| Runtime 调用 | `task_runtime_cancel.RuntimeCancelContract`（`:102`）→ `service.request_cancel:324` | 已接线 |
| 未关联任务 | `read_linked_runtime_run` 返回 `None` → 不产生任何 Runtime 调用 | 已实现 |
| 重复取消 | `_already_cancelled:171` 幂等 | 已实现 |
| cancel 后 approval 终态 | 无对应实现 | **Blocked by `G0-DEC-001`**，本卡只登记 |

---

## 5. G0 依赖登记（只登记，不实现）

| 映射列 | 阻塞决策 | 当前允许动作 |
| --- | --- | --- |
| `waiting_approval` → Task 状态不变；`approval_granted` / `approval_rejected` marker；cancel 后 approval 终态 | `G0-DEC-001` | 只登记字段缺口；`AG-G2-AUTO-004-A01` 不得断言该子场景终态 |
| `unattended_attempts` → `idempotency_key`；跨实例同触发窗口只创建一个 Run | `G0-DEC-002` | 只登记；`AG-G2-AUTO-003-A01` 明确排除持久化 claim / lease / fencing |
| `runtime_run_cursor` / `sequence` / `event_id`；流式续读与 gap | `G0-DEC-003` | 只登记；`AG-G2-AUTO-003-A01` 不得引入流式帧 / cursor / gap |
| `TaskExecutionFailedEvent.attempt_count` / `retryable`；resume → 新 attempt；重启后再投影 | `G0-DEC-004` | 只登记；`AG-G2-AUTO-003-A01` 不得实现恢复或历史迁移 |

---

## 6. A03 是否可创建

**可以。** 依据：

1. **真实旧文件可写出**：`backend/app/gateway/unattended_task_pipeline.py`（收敛点 `:599 _maybe_run_via_runtime`，`:636 _advance_unattended_task_impl`）、`backend/app/gateway/task_queue_runner.py`（tick `:71`，跳过分支 `:119`）。
2. **已存在的 Runtime 调用点**：`task_runtime_optin.establish_runtime_run`（生产已接线）、`default_runtime_contract()`（返回 `RuntimeService`，公开方法 `get_run_status`）。
3. **唯一业务事实源**：任务行 `status` + `execution_history`（`unattended_task_pipeline._patch_task:181` 是唯一写路径）。
4. **不依赖未冻结 G0 决策**：A03 只做状态投影与终态回写，不含 cancel / approval 耦合、claim、cursor / gap、recovery。
5. **修改范围可限制**：生产文件 2 个（`unattended_task_pipeline.py`、`task_queue_runner.py`）+ 新增 1 个测试文件。
6. **验收命令明确**：
   ```bash
   cd backend && PYTHONPATH=. .venv/bin/pytest -q app/tests/test_task_runtime_terminal_writeback.py app/tests/test_task_runtime_api_reads.py
   ```

### 6.1 A03 可复用的已实现模块（均零生产调用点，见 A01 §4）

`task_runtime_projection.project_runtime_status`、`task_runtime_cursor.advance_runtime_cursor`、`task_runtime_reconcile.reconcile_linked_task_runtime`、`task_runtime_degrade.reconcile_task_runtime_safely`、`task_runtime_result.sanitize_result_summary`。

### 6.2 A03 明确排除

- 新建 Run 建立路径（`433a9df` / `AG-G2-AUTO-003-B01` 已完成，不可重派）；
- 修改读取出口 `routers/tasks.py`（`GET /api/tasks/{task_id}`、`/execution-history`、`/runtime` 均已存在）；
- Event v1 流式帧 / cursor / gap；cancel 与 approval 语义；`TaskStatus` 枚举；前端；LangGraph 主链；schema / migration。

---

## 7. 未知项 / 缺口（显式列出，不补猜）

| 项 | 现状 | 处置 |
| --- | --- | --- |
| `RuntimeContract.create_run` 未声明 `org_id` | `qagent_runtime/contract.py:32` 与 `service.create_run:70` 不一致；`task_runtime_optin.RuntimeRunContract:127` 自带 Protocol 规避 | 登记为缺口，本批不改契约 |
| TC 事件 `retryable` / `attempt_count` / `subtask_ids` / `cancelled_by` / `resumed_by` | Runtime Event v1 无对应字段 | 登记为不一一对应项（§3） |
| `waiting_approval` 下任务状态的最终收敛 | 冻结语义为「不变」，但取消与审批竞态的最终状态未定 | `Blocked by G0-DEC-001` |
| 跨实例同触发窗口的 Run 唯一性 | 现有去重依赖持久化 linkage + Runtime 幂等键，无 leader / lease / fencing | `Blocked by G0-DEC-002` |
| 重启后 Runtime 状态自动再投影 | `task_runtime_reconcile` / `task_runtime_recovery` 已实现但零生产调用点 | `Blocked by G0-DEC-004`；本卡只登记 |
