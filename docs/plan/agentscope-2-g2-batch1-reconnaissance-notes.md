# G2 第一批拆卡侦察与实例化记录（非权威）

- 文档角色：**非权威**。只记录「剩余业务迁移第一批（Automation / Task Center、数字员工 / Agent、Goal / 协同）」拆卡时实际核实到的入口、事实源、状态机与 Runtime 接点，以及每张卡为何可实例化或为何必须停在上游。
- 权威入口：阶段、依赖、放行门槛以 [路线图](./agentscope-2-enterprise-runtime-roadmap.md) 为准；`AG-*` 卡片的唯一来源是 [执行计划](./agentscope-2-enterprise-runtime-execution-plan.md)。**本文件不是派工来源**，不得据此直接派工。
- 基线：`2fd4dff2f839593ea195c01473905994e9022b9b`（分支 `codex/agentscope-runtime`）。
- 侦察方式：受控 `rg`（限定 `backend/app/gateway`、`backend/app/channels`、`backend/packages/harness/evoflow`）+ 精确读取；未做全仓遍历、未建立索引、未使用 CodeSynapse。
- 范围声明：本次只改计划文档，未修改任何源码、测试、schema、migration、依赖或锁文件。文中行号对应上述基线 HEAD，仅用于定位；后续改动这些文件需重新核对。

---

## 1. Automation / Task Center

### 1.1 真实入口（既有 API，必须保持兼容）

| 入口 | 位置 |
| --- | --- |
| 自动化规则 / 调度 | `backend/app/gateway/routers/automation_scheduler.py`（`/api/automation`，含 `POST /tasks/{id}/run` 手动触发） |
| Task Center 任务 | `backend/app/gateway/routers/tasks.py`（`/api/tasks`，含 `POST /{task_id}/start`、`/stop`、`/resume`、`/restart`、`/cancel`、`/retry`、`GET /{task_id}/execution-history`、`GET /{task_id}/runtime:2674`、`GET /queue/status`、`POST /queue/tick`） |
| 状态出口 | `backend/app/gateway/routers/events.py`（`/api/events`，`/tasks/{task_id}/stream` 等 SSE） |

### 1.2 旧事实源与状态机

- 规则定义双写：TOML `~/.evoflow/tasks/automations` 为源，`evoflow_automations` 为 PostgreSQL 镜像（`backend/packages/harness/evoflow/persistence/automation_repositories.py`）。
- 运行记录：`evoflow_automation_runs`，写入集中在 `automation_repositories.append_automation_run`，调用方均在 `backend/app/gateway/automation_runner.py`。
- 任务行：既有 project storage（`unattended_task_pipeline._save_task_row` / `_patch_task`）。
- 状态机：`backend/packages/harness/evoflow/collab/models.py:59 TaskStatus`（`inbox / pending / planning / planned / executing / paused / reviewed / completed / failed / cancelled`）与 `:23 CollabPhase`；迁移规则在 `backend/packages/harness/evoflow/collab/state_transitions.py`（`can_transition:102`、`validate_transition:116`、`get_allowed_transitions:130`）。
- 既有事件词汇：`backend/app/gateway/events/task_events.py` 的 `TaskAuthorizedEvent`、`TaskExecutionStartedEvent`、`TaskExecutionFailedEvent`、`TaskCancelEvent`、`TaskCancelledEvent`、`TaskResumeEvent`。

### 1.3 Runtime 接点（本次核实的全部生产接线，共 3 处）

| 作用 | 位置 |
| --- | --- |
| 建立 / 复用 Run | `backend/app/gateway/unattended_task_pipeline.py:599 _maybe_run_via_runtime`（`:608` 引入 `task_runtime_optin`，`:614` 调 `establish_runtime_run`，`:625` 回写任务行） |
| 队列 tick 跳过已关联任务 | `backend/app/gateway/task_queue_runner.py:119 decide_runtime_pickup` |
| 取消已关联任务 | `backend/app/gateway/routers/tasks.py:1919 _cancel_linked_runtime_run_if_any`（`:1928-1930` 引入 `task_runtime_optin` / `task_runtime_cancel` / `task_runtime_context`） |

可复用的 Runtime 公开契约：`backend/app/qagent_runtime/service.py` 的 `create_run:70`（接受 `org_id`）、`start_run:85`、`get_run_status:301`、`stream_events:304`、`request_cancel:324`、`resume_run:337`、`get_result:374`；事件词汇 `backend/app/qagent_runtime/events.py:13 EventType`、`:29 TERMINAL_STATUSES`。

> 注意：`backend/app/qagent_runtime/contract.py:32 RuntimeContract.create_run` **未声明 `org_id`**，而 `service.create_run` 接受该关键字。`task_runtime_optin` 为此自带 `RuntimeRunContract` Protocol（含 `org_id`）并在创建时传入可信组织。该差异只登记为缺口，本批不改契约。

### 1.4 已实现但零生产调用点（A03 的唯一依据）

以下模块均已实现并有单测，但除测试与彼此引用外**没有任何生产调用点**：

`task_runtime_projection.py`（`project_runtime_status:354`、`build_runtime_history_record:322`）、`task_runtime_event_bridge.py`（`apply_runtime_event:128`）、`task_runtime_cursor.py`（`read_runtime_cursor:139`、`advance_runtime_cursor:164`）、`task_runtime_recovery.py`（`recover_task_stream:200`）、`task_runtime_reconcile.py`（`reconcile_linked_task_runtime:102`）、`task_runtime_degrade.py`（`assess_runtime_link:173`、`reconcile_task_runtime_safely:264`）、`task_runtime_asset.py`、`task_runtime_result.py`、`task_runtime_input_snapshot.py`、`task_runtime_failure.py`。

后果：Run 建立后，队列 tick 因 `decide_runtime_pickup` 返回 `already_scheduled` 而不再推进该任务（`task_runtime_schedule.py:103`），且没有任何代码把 Run 状态投影回任务行 —— 因此 `completed` / `failed` / `cancelled` / `timed_out` 终态与结果**当前不会出现在既有 `GET /api/tasks/{task_id}` 与 `/execution-history` 中**。按执行计划 §2.5.3 的 G2.1 单卡完成标准，该路径尚未闭环。

### 1.5 实例化结论

- 已核实存在真实旧文件与**已存在**的 Runtime 调用点，因此实例化 `AG-G2-AUTO-003-A01`（终态回写与既有读取出口）、`AG-G2-AUTO-004-A01`（幂等 / 失败 / 终态不倒退定向测试）、`AG-G2-AUTO-005-A01`（真实 HTTP 手工验收 Runbook）。
- **不重派**：Run 建立与关联（`433a9df`，`AG-G2-AUTO-003-B01`）已完成；A03 只补其未接线部分。
- 仍不实例化：Event 流式续读 / cursor / gap（`G0-DEC-003`）、跨实例触发幂等（`G0-DEC-002`）、cancel 与 approval 耦合（`G0-DEC-001`）、重启对账与数据对账、灰度观察期（`G2-AUTO-006`）。
- 本域三条未迁移旧路径保持原样，且 A03 不覆盖：App Runner 绑定工作流（`automation_runner._run_bound_app_for_automation`）、prompt-only 默认直连 LangGraph（`_run_one_task_direct_langgraph`）、调度器自身不产生 Run。

---

## 2. 数字员工 / Agent

### 2.1 真实入口

| 入口 | 位置 |
| --- | --- |
| 员工 / 主动性 API | `backend/packages/harness/evoflow/proactive/router.py`（`APIRouter(prefix="/api/proactive")`：`/{agent_code}/dispatch`、`/{agent_code}/heartbeat`、`/{agent_code}/pause`、`/{agent_code}/resume`、`/{agent_code}/stop`、`/{agent_code}/work-board`、`/{agent_code}/performance`、`/{agent_code}/cost`），经 `backend/app/gateway/router_registry.py:126-128` 挂载 |
| 平台动作入口 | `backend/packages/harness/evoflow/admin/platform_actions.py:349-368` 的 `employees` 动作组（`list`、`get`、`hire`、`update`、`pause`、`resume`、`stop`、`archive`、`worklog`、`trail`），处理函数 `backend/packages/harness/evoflow/admin/platform_handlers.py:747-842` |
| 员工服务实现 | `backend/packages/harness/evoflow/admin/employees.py`（`hire:441`、`update_role:572`、`pause_role:706`、`stop_role:725`、`resume_role:754`、`archive_role:789`、`worklog:961`、`round_trail:1142`、`dispatch:1245`、`wake:1402`） |
| 后台心跳循环 | `backend/app/gateway/background_startup.py:1030-1033 start_proactive_runner`（`backend/packages/harness/evoflow/proactive/runner.py`） |
| 角色配置 CRUD | `backend/app/gateway/routers/agents.py`（`list_agents:502`、`create_agent_endpoint:739`、`update_agent:848`、`delete_agent:1137`）—— 配置入口，非执行入口 |

### 2.2 旧事实源与状态机

- `evoflow_proactive_roles`（schema `:1175`）：`status`（`active` / `paused` / `stopped` / `archived`）、`heartbeat_rrule`、`next_heartbeat_at`、`last_heartbeat_at`、`reports_to`。
- `evoflow_proactive_initiatives`（schema `:1147`）：`status`、`round_id`、`goal`、`outcome`、`approval_id`、`approved_by`、`execution_thread_id`、`execution_result`、`config_autonomy_level`。
- `evoflow_proactive_approvals`（schema `:1117`）：`status`、`decided_by`、`decided_at`、`escalation_level`、`task_id`。
- `evoflow_agents`（schema `:174`）、`evoflow_agent_runtime`（schema `:162`：`status`、`current_task_id`、`progress`、`last_heartbeat`）。
- 工作项落点：`evoflow_collab_tasks`（schema `:553`）。

### 2.3 旧执行链与 Runtime 接点

- `evoflow/proactive/engine.py`（`ProactiveEngine.think`）→ `evoflow/proactive/execution_bridge.py`（`ExecutionBridge`，按 action_type 路由到 LangGraph lead_agent / supervisor 委派 / 直连 LangGraph）→ `evoflow/proactive/decision_gate.py`（审批）→ `evoflow/proactive/work_items.py`（工作项落为 collab Task）。
- **`backend/packages/harness` 全目录没有任何 `qagent_runtime` 或 `agentscope` 引用（核实为 0）**。因此本域不存在可复用的 Runtime 调用点。

### 2.4 实例化结论

- 实例化 `AG-G2-AGENT-001-A01`（补真实入口锚点）与 `AG-G2-AGENT-002-A01`（字段级映射，G0 列只登记）。
- **不实例化 A03～A05**：A01 / A02 无法写出「已存在的 Runtime 调用点」，且实现语义受 `G0-DEC-001～004` 阻塞。`G2-AGENT-003`～`006` 保持未实例化。

---

## 3. Goal / 协同

### 3.1 真实入口

| 入口 | 位置 |
| --- | --- |
| Goal 会话 | `backend/app/gateway/routers/goal.py:13`（`/api/goal`：`POST /start:160`、`/{session_id}/stop:205`、`/pause:230`、`/resume:251`、`/end:272`、`/after-chat-turn:293`、`POST /feedback:457`、`GET /{session_key}/status`、`/history`） |
| 协同 / 审批 | `backend/app/gateway/routers/collab.py:23`（`/api/collab`：`GET /threads/{thread_id}/tool-approval/pending:385`、`POST /threads/{thread_id}/tool-approval:405`、`POST /threads/{thread_id}/tool-approval/cancel:735`、`GET` / `PUT /threads/{thread_id}:776,825`、`GET /tasks/{main_task_id}/subtasks/{subtask_id}/history:880`） |
| 服务实现 | `backend/app/channels/services/goal_service.py`（`_sync_session_from_goal_state:537`、`_run_goal_graph:608`、`ensure_goal_stream_loop:808`、`cancel_current_run:1344`、`pause_goal:1369`、`resume_goal:1389`、`end_goal:1411`、`stop_goal:1434`、`_persist_session_snapshot:1547`、`recover_persisted_sessions:1654`、`apply_user_steering:1923`、`submit_feedback:1965`） |

### 3.2 旧事实源与状态机

- `evoflow_goal_sessions`（schema `:676`）：`goal_status`、`status`、`goal_revision`、`continuation_suppressed`、`step_count`、`last_run_id`、`pending_feedback`、`completion_outcome`、`ended_at`。
- 协同：`evoflow_collab_tasks`（schema `:553`）、`evoflow_collab_subtasks`（`:503`）、`evoflow_collab_peer_messages`（`:447`：`status`、`wake_scheduled`、`wake_round_id`、`answered_at`、`expires_at`）、`evoflow_collab_task_execution_history`（`:544`：`event_json`）、`evoflow_collab_task_deps`（`:536`）。
- 协同唤醒调度：`backend/packages/harness/evoflow/collab/peer/scheduler.py`（`schedule_peer_wake`、`enqueue_peer_wake_if_busy`、`drain_peer_wake_queue`）。
- 审批：`evoflow_proactive_approvals`（`:1117`）、`evoflow_tool_approvals`（`:1407`）、`evoflow_tool_approval_grants`（`:1399`）、`evoflow_tool_approval_audit`（`:1387`）。
- 授权 / 状态机：`backend/packages/harness/evoflow/collab/authorize_execution.py`（`is_task_execution_authorized:20`、`authorize_main_task_execution:33`、`revoke_main_task_execution_authorization:261`）、`state_transitions.py`、`models.py:59 TaskStatus`、`:23 CollabPhase`。

### 3.3 旧执行链与 Runtime 接点

- `goal_service._run_goal_graph` 经 `LangGraphClient` 跑 GoalController → MainAgent；`cancel_current_run` / `pause_goal` / `resume_goal` / `end_goal` / `stop_goal` 为会话级控制入口。
- **`backend/packages/harness` 全目录没有任何 `qagent_runtime` 或 `agentscope` 引用（核实为 0）**；`goal_service.py` 亦无 Runtime 调用。因此本域不存在可复用的 Runtime 调用点。

### 3.4 实例化结论

- 实例化 `AG-G2-GOAL-001-A01`（补真实入口锚点）与 `AG-G2-GOAL-002-A01`（字段级映射，G0 列只登记）。
- **不实例化 A03～A05**：无已存在的 Runtime 调用点，且 cancel / 执行授权 / Event / recovery 语义均未冻结。`G2-GOAL-003`～`006` 保持未实例化。

---

## 4. 本批实例化一览

| 域 | 新增 / 更新卡 | 是否含实现 |
| --- | --- | --- |
| Automation / Task Center | `AG-G2-AUTO-001-A01`（补锚点）、`AG-G2-AUTO-002-A01`（补锚点）、**新增** `AG-G2-AUTO-003-A01` / `AG-G2-AUTO-004-A01` / `AG-G2-AUTO-005-A01` | A03 为实现卡（终态回写），A04 为测试卡，A05 为验收 Runbook |
| Agent / 数字员工 | `AG-G2-AGENT-001-A01`（补锚点）、**新增** `AG-G2-AGENT-002-A01` | 否（只读证据卡） |
| Goal / 协同 | `AG-G2-GOAL-001-A01`（补锚点）、**新增** `AG-G2-GOAL-002-A01` | 否（只读证据卡） |

## 5. 本批明确排除

Chat / Live Run（`3c7aa5d`）、Apps / Workflow 首闭环与稳定化（至 `4c8c030`）、Automation Run 建立与关联（`433a9df`）均已完成，不重派；HA、多实例、分布式协调、备份恢复、压测、PG 运维、历史迁移，以及 `G0-DEC-001`～`004` 的签署与实现，均不在本批范围。
