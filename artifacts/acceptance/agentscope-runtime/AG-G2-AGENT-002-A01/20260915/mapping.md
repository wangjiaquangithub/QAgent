# AG-G2-AGENT-002-A01 — 数字员工 / Agent 对象 ↔ Runtime Run / Event / Result / Cancel 字段级映射

- 卡片：`AG-G2-AGENT-002-A01`（母任务 `G2-AGENT-002`）
- 日期 / attempt：2026-09-15 / `20260915`
- 起始 HEAD：`2d65e05586a67d088f082b91b1f54681d5c5ffb9`（分支 `codex/agentscope-runtime`）
- 前置：`AG-G2-AGENT-001-A01`（`../AG-G2-AGENT-001-A01/20260915/inventory.md`）
- 验收命令（卡片原文）：
  ```
  rg -n "evoflow_proactive_(roles|initiatives|approvals)|heartbeat_rrule|next_heartbeat_at|round_id|approval_id|execution_result|agent_runtime" \
     backend/packages/harness/evoflow/persistence/schema.py \
     backend/packages/harness/evoflow/proactive backend/packages/harness/evoflow/admin
  ```
  原始输出：`acceptance-rg.txt`（545 行，与本文件同目录）。
- 本卡**只读**：未修改任何源代码或计划外文档。**未实现任何接入**。

---

## 0. 结论先行

| 问题 | 结论 |
|---|---|
| 本域能否逐字段映射？ | **能**。§3 全部单元格都有 `文件:行` 证据，无一处需要猜测 |
| A03 是否可创建？ | **不可创建**。本域 Runtime 接点为 **0**（A01 §5），不满足 roadmap §5.1「A03～A05 需同时具备真实旧文件与**已存在的 Runtime 调用点**」 |
| 阻塞 A03 的是不是只有"缺接点"？ | 不是。即使补了接点，§4 有 **9 个字段** 分别落在 `G0-DEC-001`～`004` 上，未冻结前不得选定实现方案 |
| 是否有已冻结可复用的语义？ | 有 1 条：`cancelled` 终态与 Runtime 的 `request_cancel` 语义一致（§3.5），且 Automation 域已验证过该路径 |

---

## 1. Runtime 侧契约（只登记，不修改）

### 1.1 表与列（`backend/app/qagent_runtime/models.py`）

| 表 | 行 | 列 |
|---|---|---|
| `qagent_runs` | `models.py:10-42` | `run_id:14`、`org_id:15`（`server_default="local"`）、`task_id:16`、`status:17`、`version:18`、`input_payload:19`、`plan_payload:20`、`result_payload:21`、`error_payload:22`、`recovery_context:23`、`executor_key:24`（默认 `qagent.server.no_device.v1`）、`idempotency_key:25`、`resume_count:26`、`last_started_at:27`、`execution_claim:28`、`execution_epoch:29`、`execution_claimed_at:30`、`completed_at:31`、`created_at:32`、`updated_at:33`；唯一约束 `uq_qagent_runs_org_idempotency_key(org_id, idempotency_key):34-38` |
| `qagent_run_events` | `models.py:44-56` | `event_id:47`、`run_id:48`、`sequence:49`、`type:50`、`occurred_at:51`、`payload:52`；唯一约束 `uq_qagent_run_events_run_sequence(run_id, sequence):53` |
| `qagent_approvals` | `models.py:58-70` | `approval_id:61`、`org_id:62`、`run_id:63`、`status:64`、`requested_at:65`、`decided_at:66`、`decided_by:67`、`reason:68` |
| `qagent_recovery_points` | `models.py:72-82` | `recovery_point_id`、`run_id`、`checkpoint_key`、`sequence`、`context`、`created_at` |
| `qagent_run_assets` | `models.py:83-94` | `asset_id:85`、`run_id:86`、`asset_type:87`、`uri:88`、`name:89`、`content_type:90`、`metadata:91`、`created_at:92` |

### 1.2 服务方法返回结构（`backend/app/qagent_runtime/service.py`）

| 方法 | 行 | 返回 |
|---|---|---|
| `create_run` | `:70` | `repository.create_run(...)` 的 dict（含 `run_id` / `status` / `org_id`，A05 实测） |
| `start_run` | `:85` → `_start_run_locked:93` | `status()` 结构；`created`/`queued` → 认领 `planning` 并 `_plan_and_wait_locked:115` |
| `get_run_status` | `:300` | 转发 `status()` |
| `stream_events` | `:304` | 逐条 yield `event_dict(event)`；终态且无新事件时 break |
| `request_cancel` | `:324` | `transition(run_id, "cancelled", EventType.RUN_CANCELLED, {}, from_statuses=INCOMPLETE_STATUSES)` 后返回 `status()` |
| `resume_run` | `:337` | `increment_resume` 后按当前状态分支；终态直接返回 `status()` |
| `get_result` | `:374` | `{run_id, status, result(=result_payload), assets, error(=error_payload)}` |
| `status` | `:384` | `{run_id, task_id, status, version, created_at, updated_at, resume_count, plan(=plan_payload), approval(=get_latest_approval), error(=error_payload)}` |

### 1.3 事件词汇（`backend/app/qagent_runtime/events.py`）

- `EventType`（`:13-26`）：`run.created`、`run.queued`、`run.planning`、`run.waiting_approval`、`approval.granted`、`approval.rejected`、`run.running`、`run.executing`、`run.completed`、`run.failed`、`run.cancelled`、`asset.available`。
- `TERMINAL_STATUSES = {"completed","failed","cancelled","timed_out"}`（`:29`）
- `INCOMPLETE_STATUSES = {"created","queued","planning","waiting_approval","running","executing"}`（`:30`）
- `event_dict`（`:37`）输出 `{event_id, run_id, sequence, occurred_at, type, payload}`。

### 1.4 审批状态取值（`backend/app/qagent_runtime/repository.py`）

- 初始写入 `status="requested"`（`:334`，`set_plan_and_approval`）。
- 决定写入 `granted` / `rejected`（`:387`、`:419` 的 `status not in {"granted","rejected"}` 校验）。
- **Runtime 侧 approval 只有 `requested` / `granted` / `rejected` 三个值**。

### 1.5 契约缺口（只登记）

`backend/app/qagent_runtime/contract.py:32 RuntimeContract` 的 `create_run:33` **未声明 `org_id`**。
注意与 Gateway 侧的 `RuntimeRunContract`（`backend/app/gateway/task_runtime_optin.py:137-141`，**已声明** `org_id` 并校验回显 `:434-439`）区分：
两者是不同层的协议，前者是 Runtime 自己的窄接口，后者是 Task Center 使用的窄接口。本卡不改契约。

---

## 2. 旧域对象（本域事实源）

### 2.1 `ProactiveRole`（`backend/packages/harness/evoflow/proactive/models.py`）

`agent_code`、`role_name`、`position_code`、`department`、`config: ProactiveRoleConfig`、
`heartbeat_rrule`（默认 `FREQ=HOURLY;INTERVAL=2`）、`heartbeat_schedule`（5 段 cron）、
`status`（`active/paused/archived/draft`）、`last_heartbeat_at`、`next_heartbeat_at`、`created_at`、`updated_at`。

### 2.2 `Initiative`（`models.py:301-330`）

`id`、`role_agent_code`、`title`、`description`、`rationale`、`action_type: InitiativeActionType`、
`risk_level: InitiativeRiskLevel`、`action_plan`、`expected_outcome`、`status: InitiativeStatus`、
`approval_id`、`approved_by`、`approved_at`、`approval_timeout_minutes`（默认 30）、
`execution_thread_id`、`execution_result`、`round_id`、`goal`、`outcome`、`created_at`、`updated_at`、
`config_autonomy_level: ProactiveAutonomyLevel`（默认 `APPROVAL_FOR_RISKY`）。

### 2.3 `Approval`（`models.py:333-355`）

`id`、`initiative_id`、`role_agent_code`、`channel`（`feishu`/`desktop`/`both`）、`feishu_message_id`、
`status: ApprovalStatus`、`decided_by`、`decided_at`、`decision_comment`、`escalation_level`（int，默认 0）、
`rejection_reason`、`task_id`、`created_at`、`updated_at`。

### 2.4 状态枚举（权威，取自 `models.py`）

| 枚举 | 行 | 值 |
|---|---|---|
| `ROLE_STATUSES` | `:267` | `active`、`paused`、`archived`、`draft` |
| `InitiativeStatus` | `:45-56` | `proposed`、`pending_approval`、`approved`、`rejected`、`executing`、`completed`、`failed`、`timeout_rejected`、`skipped` |
| `ApprovalStatus` | `:59-66` | `pending`、`approved`、`rejected`、`timeout`、`escalated` |

### 2.5 工作项落点

`evoflow_collab_tasks`（`schema.py:553`）；写点为 `proactive/work_items.py:274 create_role_work_item`，
读点为 `:382/:404/:456/:501/:587`。工作项状态取自 collab 的 `TaskStatus`（`evoflow/collab/models.py:59`）。

---

## 3. 逐字段映射

图例：**确定** = 两侧字段与语义都已由源码证明；**未知** = 本卡无法从源码得出，需决策；**G0** = 受未冻结决策阻塞。

### 3.1 角色 / 员工状态 ↔ Run 状态

| 旧字段 | 证据 | 旧值 | Runtime 对应 | 判定 |
|---|---|---|---|---|
| `ProactiveRole.status` | `models.py:294`；`ROLE_STATUSES models.py:267` | `active` | **无对应** —— Runtime 没有"长期在岗"概念，Run 是一次性的 | **未知**：需要决策"员工在岗状态"是否根本不进 Runtime（本卡倾向：不进） |
| 同上 | 同上 | `paused` | **无对应**（Run 的 `paused` 不是合法状态；`INCOMPLETE_STATUSES` 无 `paused`） | **未知** |
| 同上 | 同上 | `archived` | **无对应** | **未知** |
| 同上 | 同上 | `draft` | **无对应** | **未知** |
| `pause_role` / `resume_role` / `stop_role` | `admin/employees.py:706/754/725`；`models.py:158-163` 注释：stop 也落 `paused` | — | `resume_run:337` 名字相近但语义不同（恢复被中断的 Run，不是恢复员工值班） | **G0-DEC-004**（attempt / 续跑语义） |
| `ProactiveRole.config`（`ProactiveRoleConfig`） | `models.py:294` | 思考模式 / 自治级别等 | 若进 Runtime 只能落 `input_payload` | **未知**：payload schema 未冻结 |

**小结**：`status` 四个值与 Runtime 状态机**没有同名同义项**。把"员工状态"直接投影到"Run 状态"是语义错误，
必须显式决定哪些状态留在 Task Center 侧、哪些升格为 Run。

### 3.2 心跳 ↔ Run 生命周期

| 旧字段 | 证据 | Runtime 对应 | 判定 |
|---|---|---|---|
| `heartbeat_rrule`（默认 `FREQ=HOURLY;INTERVAL=2`） | `models.py:292`；schema `:1180` | **无对应**。Runtime 无调度字段；Automation 域把调度留在 Task Center 侧 | **确定（不映射）**：调度属 Task Center，Run 只承接一次触发 |
| `heartbeat_schedule`（5 段 cron，"same semantics as automation tasks"） | `models.py:294` | 同上 | **确定（不映射）** |
| `last_heartbeat_at` / `next_heartbeat_at` | `models.py:295-296`；schema `:1182-1183` | **无对应**。`qagent_runs.last_started_at` / `completed_at` 是"某一次执行"的时间，不是"下一次巡查"的时间 | **确定（不映射）** |
| 「同一触发窗口只创建一个 Run」 | 无源码实现（`ProactiveRunner.start:510` 为单进程循环） | 与 Automation 域 `idempotency_key_for(org_scope_key, task_id, attempt)` 同构的确定性键 | **G0-DEC-002**（跨实例执行所有权 / 触发幂等）。**未冻结前不得实现** |
| 触发源（`start_proactive_runner`） | `background_startup.py:1028-1033`；`runner.py:3262` | 无 Runtime 调用 | **确定（当前为 0）** |

### 3.3 Initiative ↔ Run

| 旧字段 | 证据 | Runtime 对应 | 判定 |
|---|---|---|---|
| `Initiative.id` | `models.py:305` | `qagent_runs.task_id`（或 `input_payload` 内的业务 id） | **未知**：需要决策 id 归属。Automation 域用 `runtime_task_id = "tc-" + sha256(org_scope_key, task_id)[:24]` 的确定性映射，本域无同类实现 |
| `Initiative.status` | `models.py:314`；`InitiativeStatus models.py:45-56` | 见下表 | 部分**确定**，部分**未知** |
| `role_agent_code` | `models.py:306` | `input_payload` 内的 agent 标识 | **确定（落 payload）**，但 payload schema 未冻结 |
| `title` / `description` / `rationale` / `expected_outcome` / `action_plan` | `models.py:307-312` | `input_payload`（`input_payload` 为 `nullable=False`，`models.py:19`） | **确定（落 payload）** |
| `action_type` / `risk_level` / `config_autonomy_level` | `models.py:309-310`、`:330` | 决定是否需要审批 → 影响 `run.waiting_approval` | **G0-DEC-001**（审批语义） |
| `execution_thread_id` | `models.py:317` | **无对应**。Runtime 不暴露 thread；`plan_payload` 是其最近似物但语义不同 | **未知** |
| `execution_result` | `models.py:318`；schema `:1163` | `qagent_runs.result_payload` | **确定（同名同义）**：`get_result:374` 直接返回 `result_payload` |
| `round_id` | `models.py:319`；schema `:1165` | **无对应列**。只能落 `input_payload` / `recovery_context` | **G0-DEC-004**（round 续跑语义）。**未冻结前不得实现** |
| `goal` / `outcome` | `models.py:320-321`；schema `:1165` | **无对应列**，落 `input_payload` / `result_payload` | **未知** |
| `approval_id` | `models.py:315`；schema `:1158` | `qagent_approvals.approval_id` | **确定（同名同义）** |
| `approved_by` / `approved_at` | `models.py:316` | `qagent_approvals.decided_by:67` / `decided_at:66` | **确定（同名同义）** |
| `approval_timeout_minutes`（默认 30） | `models.py:316` | **无对应**。Runtime approval 无超时字段 | **G0-DEC-001** |
| `created_at` / `updated_at` | `models.py:322-323` | `qagent_runs.created_at:32` / `updated_at:33` | **确定（同名同义）** |

**`InitiativeStatus` → Run 状态映射**（本卡给出的**候选**，非实现方案）：

| InitiativeStatus | 证据 | 候选 Run 状态 | 判定 |
|---|---|---|---|
| `proposed` | `models.py:48` | `run.created` / `run.queued` | **未知**：需要决策"提议即建 Run"还是"批准后才建 Run" |
| `pending_approval` | `:49` | `waiting_approval` | **G0-DEC-001** |
| `approved` | `:50` | `running` / `executing` | **G0-DEC-001** |
| `rejected` | `:51` | `failed`？还是 `cancelled`？ | **未知**：Runtime 无 `rejected` run 终态。Automation 域把 `timed_out` 收敛为 `failed`，但"人工拒绝"没有先例 |
| `executing` | `:52` | `executing` | **确定（同名）** |
| `completed` | `:53` | `completed` | **确定（同名）** |
| `failed` | `:54` | `failed` | **确定（同名）** |
| `timeout_rejected` | `:55` | **无对应**。Runtime 的 `timed_out` 是 **run** 终态，不是"因超时被拒"的审批语义 | **G0-DEC-001** |
| `skipped` | `:56`（"auto-skipped (e.g. duplicate)"） | **无对应** | **未知** |

### 3.4 审批 ↔ Runtime approval

| 旧字段 / 值 | 证据 | Runtime 对应 | 判定 |
|---|---|---|---|
| `Approval.status = pending` | `models.py:62` | `qagent_approvals.status = "requested"`（`repository.py:334`） | **确定（值不同、语义同）**：必须显式做值映射，不能同名直传 |
| `= approved` | `:63` | `"granted"`（`repository.py:387`） | **确定（值不同、语义同）** |
| `= rejected` | `:64` | `"rejected"`（`repository.py:387`） | **确定（同名同义）** |
| `= timeout` | `:65` | **无对应**。Runtime approval 无超时状态 | **G0-DEC-001** |
| `= escalated` | `:66` | **无对应**。Runtime 无升级概念 | **G0-DEC-001** |
| `escalation_level`（int，默认 0） | `models.py:346` | **无对应** | **G0-DEC-001** |
| `decided_by` / `decided_at` | `models.py:344-345` | `qagent_approvals.decided_by:67` / `decided_at:66` | **确定（同名同义）** |
| `decision_comment` / `rejection_reason` | `models.py:346`、`:349` | `qagent_approvals.reason:68`（`sa.Text()`，单列） | **确定但需合并**：两个旧字段要合成一个 `reason`，合并规则未定 → **未知** |
| `channel`（`feishu`/`desktop`/`both`） | `models.py:338` | **无对应**。渠道是投递方式，不属 Runtime | **确定（不映射）** |
| `feishu_message_id` | `models.py:339` | **无对应** | **确定（不映射）**，属 Channels 域（`AG-G2-KNOW-001-A01`） |
| `initiative_id`（注释 "legacy; empty when task_id is set"） | `models.py:336` | `qagent_approvals.run_id:63` | **未知**：`initiative_id` 与 `task_id` 二选一的语义需要决策才能定 run 关联 |
| `task_id`（"preferred over initiative_id when set"） | `models.py:352` | `qagent_approvals.run_id:63` | **未知**：同上 |
| 审批事件 | — | `approval.granted` / `approval.rejected`（`events.py:19-20`）；`run.waiting_approval`（`:17`） | **确定（事件存在）**，但"审批与 cancel 竞态"归 **G0-DEC-001** |

### 3.5 取消

| 旧行为 | 证据 | Runtime 对应 | 判定 |
|---|---|---|---|
| `stop_role`（停止当前轮次） | `admin/employees.py:725`；`models.py:158-163`：skip + 落 `paused`，**不是**取消 Runtime 执行 | `request_cancel:324`（`from_statuses=INCOMPLETE_STATUSES` → `cancelled` + `run.cancelled` 事件） | **G0-DEC-001**：旧 `stop` 与 Runtime `cancel` 不是同一语义，不能直连 |
| `pause_role` / `resume_role` | `admin/employees.py:706/754` | 无对应（Runtime 无 pause 状态） | **确定（不映射）** |
| 取消后 approval 的处理 | 无实现 | `request_cancel` 只做 run 状态转移，不触碰 `qagent_approvals` | **G0-DEC-001**（审批与 cancel 竞态）。Automation 域已把该子场景列入阻塞清单（execution-plan §2.6.4 第 1 行） |
| 终态防回退 | Automation 域已实现（`project_runtime_status` 的 stage rank + `_is_duplicate`） | 本域无实现 | **确定（能力已存在，可复用）** |

### 3.6 工作项 ↔ collab Task

| 旧对象 | 证据 | Runtime 对应 | 判定 |
|---|---|---|---|
| `create_role_work_item` | `work_items.py:274` | 无 | **确定（不映射）**：工作项落 `evoflow_collab_tasks`，是 Task Center 侧对象 |
| 工作项状态 | `evoflow/collab/models.py:59 TaskStatus` | 无 | **确定（不映射）** |
| `task_needs_approval` | `work_items.py:848` | 决定是否产生 approval | **G0-DEC-001** |
| `all_referenced_tasks_done` / `is_work_item_done` | `work_items.py:247/124` | 无 | **确定（不映射）** |

### 3.7 结果与错误引用

| 旧字段 | 证据 | Runtime 对应 | 判定 |
|---|---|---|---|
| `Initiative.execution_result` | `models.py:318` | `qagent_runs.result_payload:21`；读出口 `get_result:374` 的 `result` | **确定（同名同义）** |
| 失败原因 | `engine.py:120 _is_execution_failure(result)` 判定字符串 | `qagent_runs.error_payload:22`；读出口 `get_result:374` 的 `error` | **确定（目标字段存在）**，但旧侧是"字符串判定失败"，新侧是结构化 payload → 转换规则 **未知** |
| 资产 | `proactive/artifacts.py` | `qagent_run_assets`（`models.py:83-94`）；事件 `asset.available`（`events.py:25`） | **确定（目标存在）**，本域是否有资产产出未在本卡核实 → **未知** |
| 恢复点 | 无对应实现 | `qagent_recovery_points`（`models.py:72-82`） | **G0-DEC-004** |

### 3.8 幂等键

| 侧 | 现状 | 判定 |
|---|---|---|
| Automation 域 | `idempotency_key_for(org_scope_key, task_id, attempt)` → `"tc:" + sha256(...)[:32]`；持久化在任务行 extras 的 `runtime_run_linkage` | 可复用**模式**，不可复用**实现**（键前缀与 attempt 语义不同） |
| 本域 | **无任何实现**（Runtime 接点 = 0） | **G0-DEC-002**：键必须能表达"同一触发窗口只创建一个 Run"，而这正是未冻结的执行所有权语义 |

---

## 4. G0 依赖汇总（逐列，不得实现）

| 决策 | 涉及的映射项（§3 中的行） | 未冻结时的允许动作 |
|---|---|---|
| `G0-DEC-001` | 3.3 `action_type`/`risk_level`/`config_autonomy_level`、`approval_timeout_minutes`；3.4 `timeout`/`escalated`/`escalation_level`/`reason` 合并/`initiative_id`↔`task_id`；3.5 `stop_role` 与 `cancel` 的语义边界、取消后 approval 处理；3.6 `task_needs_approval` | 只登记，不选方案 |
| `G0-DEC-002` | 3.2 心跳触发窗口与"同一窗口只创建一个 Run"；3.8 幂等键来源 | 只登记，不实现 claim / lease / fencing |
| `G0-DEC-003` | 3.3 `execution_thread_id`（thread ↔ 事件流的等价性）；事件 `sequence` / cursor / gap 的业务映射 | 只登记；不得引入流式帧 / cursor / gap |
| `G0-DEC-004` | 3.1 `resume_run` 语义；3.3 `round_id`；3.7 恢复点 | 只登记，不实现恢复或历史迁移 |

---

## 5. A03 是否可创建：**否**

roadmap §5.1 的 A03～A05 实例化门槛要求**同时**具备：

| 门槛 | 本域现状 | 是否满足 |
|---|---|---|
| 有真实 HTTP / 页面 / worker 或 service 调用入口 | ✅ `/api/proactive`（43 端点）、`/api/platform employees.*`（10 动作）、后台心跳 | 满足 |
| 已定位唯一业务事实源和既有写路径 | ✅ A01 §2/§3 | 满足 |
| **已找到可复用 Runtime / AgentScope 调用点** | ❌ **本域 Runtime 接点 = 0**（A01 §5，`rg` 计数为 0） | **不满足** |
| 不依赖未冻结的 `G0-DEC-001～004` | ❌ §4 有 9 类映射项分别落在四项决策上 | **不满足** |
| 可把修改范围限制在 ≤ 3 个既有生产文件 + 1 个新测试文件 | ❌ 任何接入都需**新增** bridge / 开关 / linkage 语义（本域不存在可改的既有接线） | **不满足** |
| 有明确、定向、可运行的验收命令 | — | 无实现则无从谈验收 |

**结论**：`AG-G2-AGENT-003-A01`～`A05` **不得创建**。本域后续动作只有两类：
(1) 等 `G0-DEC-001～004` 冻结；(2) 等负责人决定是否要先做"Agent 域 Runtime 接点"这张前置卡（该卡本身超出本批授权）。

---

## 6. 未知项清单（不得补猜，留给决策）

| # | 未知项 | 为什么不能猜 |
|---|---|---|
| U1 | `ProactiveRole.status` 四值是否进入 Runtime | Runtime 状态机没有对应枚举；"员工在岗"与"Run 存活"不是同一生命周期 |
| U2 | `Initiative.id` 与 Runtime `task_id` 的 id 归属 | 两侧 id 生成规则不同；Automation 域用确定性映射，本域无先例 |
| U3 | `rejected` initiative 应收敛为 `failed` 还是 `cancelled` | Runtime 无 `rejected` run 终态；Automation 域只有 `timed_out → failed` 的先例，不能外推 |
| U4 | `skipped` initiative 的 Runtime 表达 | 无对应状态 |
| U5 | `decision_comment` + `rejection_reason` → 单个 `reason` 的合并规则 | 两侧结构不同（旧侧两列，新侧一列 `Text`） |
| U6 | `initiative_id` / `task_id` 二选一如何决定 run 关联 | 旧注释只说"task_id 优先"，未说明何时二者都空 |
| U7 | `execution_thread_id` 的 Runtime 等价物 | Runtime 不暴露 thread；`plan_payload` 语义不同 |
| U8 | 失败原因从"字符串判定"到结构化 `error_payload` 的转换规则 | 旧侧 `engine.py:120` 是启发式字符串判定 |
| U9 | 本域是否产生 `asset` | 未在本卡核实 `proactive/artifacts.py` 的产出是否进入 Runtime 资产域 |

## 7. 本卡是否产生代码改动

**否**。只写了 `artifacts/acceptance/agentscope-runtime/AG-G2-AGENT-002-A01/20260915/` 下的
`mapping.md` 与 `acceptance-rg.txt`。未修改任何源代码、测试、schema、migration 或计划外文档；
未创建或删除 branch / worktree；未执行任何被任务包禁止的 git 操作。
