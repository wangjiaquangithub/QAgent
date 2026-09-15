# AG-G2-GOAL-002-A01 — Goal / 协同对象 ↔ Runtime Run / Event / Result / Cancel 字段级映射

- 卡片：`AG-G2-GOAL-002-A01`（母任务 `G2-GOAL-002`；映射本身可执行，**实现部分 `Blocked by G0-DEC-001～004`**）
- 日期 / attempt：2026-09-15 / `20260915`
- 起始 HEAD：`582318897296b9e275d1dd8aad28a35438a31cd1`（分支 `codex/agentscope-runtime`）
- 前置：`AG-G2-GOAL-001-A01`（`../AG-G2-GOAL-001-A01/20260915/dependency-inventory.md`）
- 验收命令（卡片原文）：
  ```
  rg -n "goal_status|continuation_suppressed|execution_authorized|event_json|wake_scheduled|TaskStatus|CollabPhase" \
     backend/app/channels/services/goal_service.py backend/packages/harness/evoflow/collab \
     backend/packages/harness/evoflow/persistence/schema.py
  ```
  原始输出：`acceptance-rg.txt`（312 行，与本文件同目录）。
- 本卡**只读**：未新增字段、接口、schema、状态机或 fallback；未实现 cancel / recovery / claim / cursor；
  未改动 `state_transitions.py` 的既有迁移规则；未触碰 HA、备份、灾备、压测、历史迁移或其他业务域。

图例：**确定** = 两侧字段与语义已由源码证明；**未知** = 本卡无法从源码得出；**G0-nnn** = 受未冻结决策阻塞，只登记不给方案。

---

## 0. 结论先行

| 问题 | 结论 |
|---|---|
| 逐字段映射是否可完成？ | **可完成**。§2~§3 每格都有 `文件:行` 证据，无一处臆造 |
| A03 是否可创建？ | **不可创建**（§5）。本域 Runtime 接点为 0，且四项 G0 决策全部命中 |
| 是否发现了 A01 遗留的未知项答案？ | **是 1 项**：`goal_status` 全集 = `active / paused / completed / cleared`（`app/channels/models/goal.py:68`），A01 §8 U1 已由本卡关闭 |
| 是否有字段名在两侧不一致、必须显式转换？ | **是**（§3.4）：schema 的 `step_count` 与模型层的 `current_step` 指同一语义但不同名 |

---

## 1. Runtime 侧契约（只登记，不修改）

沿用 `AG-G2-AGENT-002-A01` §1 已核实的登记，不重复展开：

- 表与列：`qagent_runs`（`models.py:10-42`）、`qagent_run_events`（`:44-56`）、`qagent_approvals`（`:58-70`）、`qagent_recovery_points`（`:72-82`）、`qagent_run_assets`（`:83-94`）
- 服务返回结构：`status:384`、`get_result:374`、`request_cancel:324`、`resume_run:337`、`stream_events:304`、`start_run:85`、`create_run:70`（均在 `backend/app/qagent_runtime/service.py`）
- 事件词汇：`events.py:13 EventType`（12 值）、`:29 TERMINAL_STATUSES`、`:30 INCOMPLETE_STATUSES`、`:37 event_dict`
- 审批取值：`requested` / `granted` / `rejected`（`repository.py:334/387/419`）

---

## 2. 旧对象与事实源

| 对象 | 定义 / schema | 关键列 |
|---|---|---|
| `GoalSession` | `backend/app/channels/models/goal.py:55-86` | `id:58`、`user_id:59`、`channel_type:60`、`associated_session_key:63`、`config:64`、`status:65`（`GoalStatus`）、`current_step:66`、`goal_revision:67`、`goal_status:68`、`continuation_suppressed:69`、`error_count:73`、`last_run_at:74`、`last_error:75`、`pending_feedback:78`、`feedback_prompt:79`、`feedback_timeout_at:80`、`goal_summary:85`、`completion_outcome:86` |
| `evoflow_goal_sessions` | `schema.py:676-713` | `goal_status:691`、`goal_revision:692`、`continuation_suppressed:693`、`status:694`、`step_count:695`、`last_run_id:699`、`pending_feedback:700`、`error_count:702`、`completion_outcome:709` |
| `evoflow_collab_tasks` | `schema.py:553` | `status`、`execution_authorized`、`thread_id`、`authorized_at`、`authorized_by`、`result_json`、`extra_json`、`progress`、`created_at` / `started_at` / `completed_at` |
| `evoflow_collab_subtasks` | `schema.py:503-535` | 同名列 + `worker_base_subagent` / `worker_model` / `worker_instruction` / `worker_validation` / `worker_max_retries`（默认 3） |
| `evoflow_collab_peer_messages` | `schema.py:447-465` | `message_id`、`thread_key`、`from_party`、`to_subtask_id`、`direction`（默认 `question`）、`body`、`in_reply_to`、`status`（默认 `pending`）、`visibility_json`、`wake_scheduled`（默认 0）、`wake_round_id`、`created_at`、`answered_at`、`expires_at` |
| `evoflow_collab_task_execution_history` | `schema.py:544-551` | `id`、`main_task_id`、`parent_task_id`、`subtask_id`、`sort_order`、`event_json`、`updated_at` |
| `evoflow_tool_approvals` | `schema.py:1407-1420` | `status`（默认 `pending`）、`session_key`、`thread_id`、`tool_call_id`、`tool_name`、`args_json`、`signature` |
| `evoflow_tool_approval_grants` | `schema.py:1399-1405` | `session_key`(PK)、`thread_id`、`grant_all`（默认 0）、`signatures_json` |
| `evoflow_tool_approval_audit` | `schema.py:1387-1397` | `session_key`、`thread_id`、`tool_call_id`、`tool_name`、`args_json`、`action`、`user_source`（默认 `ui`）、`created_at` |
| `evoflow_proactive_approvals` | `schema.py:1117` | 见 `AG-G2-AGENT-002-A01` §3.4 |

---

## 3. 逐字段映射

### 3.1 Goal 会话生命周期

| 旧字段 | 证据 | 旧值 | Runtime 对应 | 判定 |
|---|---|---|---|---|
| `goal_status` | `goal.py:68`；schema `:691` | `active` / `paused` / `completed` / `cleared` | **无对应**。Runtime 无"会话级生命周期"，只有 run 级状态 | **未知**：需要决策"会话生命周期"是否根本不进 Runtime（本卡倾向：不进，留在 Task Center） |
| `status`（`GoalStatus`） | `goal.py:65`；`models/goal.py:9-16` | `idle` / `running` / `waiting` / `paused` / `error` | 与 `qagent_runs.status` 部分同形：`running` 可对 `running`/`executing`；`error` 可对 `failed` | **未知**：`idle` / `waiting` / `paused` 在 Runtime 无同名项（`INCOMPLETE_STATUSES` 无 `paused`，`waiting_approval` ≠ `waiting`） |
| `goal_revision` | `goal.py:67`（"用户纠正后递增并作废旧 continuation"）；schema `:692` | 递增整数（≥1） | **无对应**。Runtime 的 `version`（`models.py:18`）是 run 版本，不是目标版本 | **G0-DEC-004**：与 attempt / 续跑语义直接耦合 |
| `continuation_suppressed` | `goal.py:69-72`（"用户停止当前 run 后为 true，禁止 Goal Controller 自动续跑"）；schema `:693` | bool | **无对应**。Runtime 没有"禁止续跑"标志；最接近的是终态本身 | **G0-DEC-004**（续跑与恢复） |
| `current_step` / `step_count` | `goal.py:66`；schema `:695` | 整数 | **无对应**。Runtime 的 `resume_count`（`models.py:26`）与 `execution_epoch`（`:29`）都不是步数 | **G0-DEC-004** |
| `last_run_id` | schema `:699` | 字符串 | `qagent_runs.run_id`（若能建立） | **未知**：这正是 A03 要解决的问题（如何关联）；在接点为 0 时无法确定 |
| `pending_feedback` | `goal.py:78`；schema `:700` | bool | **无对应**。Runtime 的 `waiting_approval` 是审批，不是"等人工反馈" | **未知**：两者是否视为同一语义需决策；若视为同一，则落 **G0-DEC-001** |
| `feedback_prompt` / `feedback_timeout_at` | `goal.py:79-80` | 字符串 / 时间戳 | **无对应** | **G0-DEC-001**（超时语义） |
| `error_count` | `goal.py:73`；schema `:702` | 整数 | **无对应**。Runtime 的失败由 `error_payload` 承载，不计数 | **G0-DEC-004** |
| `last_error` | `goal.py:75` | 字符串 | `qagent_runs.error_payload`（`models.py:22`） | **确定（目标存在）**，但旧侧是字符串、新侧是结构化 payload → 转换规则 **未知** |
| `goal_summary` / `completion_outcome` | `goal.py:85-86`；schema `:709` | 字符串 | `qagent_runs.result_payload`（`models.py:21`） | **确定（目标存在）**，字段拆分规则 **未知** |
| `created_at` / `ended_at` | `goal.py:76-77` | 时间戳（float） | `qagent_runs.created_at:32` / `completed_at:31` | **确定（语义同，类型不同：float ↔ tz-aware datetime）** |

### 3.2 `TaskStatus` ↔ Runtime 状态（10 值逐项）

`TaskStatus` 权威定义：`backend/packages/harness/evoflow/collab/models.py:59-71`。

| TaskStatus | 行 | Runtime 对应 | 判定 |
|---|---|---|---|
| `inbox` | `:60` | **无对应**（未开跑，不产生 Run） | **确定（不映射）** |
| `pending` | `:61` | `created` / `queued` | **未知**：`created` 与 `queued` 选哪个需决策（Automation 域把两者都映射为 `pending`，是反向映射） |
| `planning` | `:62` | `planning` | **确定（同名同义）** |
| `planned` | `:63` | **无对应**。Runtime 无 `planned` 状态（规划完成即进 `waiting_approval` 或 `running`） | **G0-DEC-001** |
| `executing` | `:64` | `executing`（或 `running`） | **未知**：Runtime 同时有 `running` 与 `executing` 两个值，旧侧只有一个，需决策 |
| `paused` | `:65` | **无对应**。`INCOMPLETE_STATUSES`（`events.py:30`）无 `paused` | **G0-DEC-001**（pause 与 cancel 的关系） |
| `reviewed` | `:66`（注释"legacy；写入时映射为 `completed`"） | `completed` | **确定（旧侧已声明等价）** |
| `completed` | `:67` | `completed` | **确定（同名同义）** |
| `failed` | `:68` | `failed`（`timed_out` 亦收敛为 `failed`，Automation 域已冻结该语义） | **确定** |
| `cancelled` | `:69` | `cancelled` | **确定（同名同义）** |

**反向（Runtime → TaskStatus）**：`waiting_approval` 与 `timed_out` 在旧侧无同名项，
Automation 域已冻结的处理是"`waiting_approval` 不改任务状态、`timed_out` 收敛为 `failed`"。
本卡**只登记该先例存在**，不把它外推为本域方案 —— 本域审批对象与 Automation 不同（工具审批 vs 任务授权）。

### 3.3 `CollabPhase` ↔ Runtime 状态（10 值逐项）

`CollabPhase` 权威定义：`collab/models.py:23-35`。

| CollabPhase | 行 | Runtime 对应 | 判定 |
|---|---|---|---|
| `idle` | `:26` | **无对应** | **确定（不映射）** |
| `req_confirm` | `:27` | **无对应** | **未知** |
| `planning` | `:28` | `planning` | **确定（同名）** |
| `plan_ready` | `:29` | **无对应** | **G0-DEC-001** |
| `awaiting_exec` | `:30` | `waiting_approval`？ | **G0-DEC-001**（"等待执行"与"等待审批"是否同一语义） |
| `executing` | `:31` | `executing` / `running` | **未知** |
| `verifying` | `:32`（注释"限制副作用工具"） | **无对应** | **未知** |
| `reflecting` | `:33`（注释"只读 + 编排，禁止写文件"） | **无对应** | **未知** |
| `paused` | `:34` | **无对应** | **G0-DEC-001** |
| `done` | `:35` | `completed` | **确定（语义同）** |

**关键结构事实**：`CollabPhase` 是**每线程**状态（`ThreadCollabState`，`collab/models.py:39` 起），
而 `TaskStatus` 是**每任务**状态。Runtime 是**每 Run**。三者粒度不同（线程 / 任务 / 执行），
**不存在一一对应关系** —— A03 若要做映射，必须先决定粒度对齐方案，这本身是契约级决策。

### 3.4 执行授权

| 旧字段 | 证据 | Runtime 对应 | 判定 |
|---|---|---|---|
| `execution_authorized` | `evoflow_collab_tasks` / `_subtasks`（`schema.py:553/503` 起，`INTEGER NOT NULL DEFAULT 0`） | **无对应列**。Runtime 的 `waiting_approval` + `qagent_approvals` 是"批准执行"，但语义与"用户授权该任务可执行"不同 | **G0-DEC-002**（执行授权与跨实例执行所有权） |
| `authorized_at` | 同上 | **无对应**。最接近 `qagent_approvals.decided_at:66` | **G0-DEC-002** |
| `authorized_by` | 同上 | **无对应**。最接近 `qagent_approvals.decided_by:67` | **G0-DEC-002** |
| `thread_id` | 同上 | **无对应**（Runtime 不暴露 thread） | **未知** |

> 注意：Automation 域已把"执行授权"保留在 Task Center 侧、不作为 Runtime 概念
> （`task_runtime_optin.is_task_execution_authorized` 是准入前置，不是 Runtime 状态）。
> 本卡只登记该先例，不外推。

### 3.5 执行历史 `event_json`

| 旧字段 | 证据 | Runtime 对应 | 判定 |
|---|---|---|---|
| `evoflow_collab_task_execution_history.event_json` | `schema.py:550`（`TEXT NOT NULL`，自由结构） | `qagent_run_events.payload`（`models.py:52`，JSONB） | **确定（目标存在）** |
| `sort_order` | `schema.py:549` | `qagent_run_events.sequence`（`models.py:49`，有 `UNIQUE(run_id, sequence)`） | **G0-DEC-003**：旧侧 `sort_order` 无唯一约束、无单调性保证；新侧 `sequence` 有唯一约束。**两者不能直传** |
| 历史读取出口 | `GET /api/collab/tasks/{main_task_id}/subtasks/{subtask_id}/history`（`collab.py:880`）、`GET /api/goal/{session_id}/history`（`goal.py:426`） | `stream_events:304`、`get_result:374` | **确定（两侧出口都在）**，等价性未证明 → **G0-DEC-003** |
| 事件类型 | 自由结构，无枚举证据 | `EventType`（`events.py:13-26`，12 值） | **G0-DEC-003** |

### 3.6 协同消息

| 旧字段 | 证据 | Runtime 对应 | 判定 |
|---|---|---|---|
| `status` | `schema.py:456`（`NOT NULL DEFAULT 'pending'`） | **无对应**。Runtime 无"消息"概念 | **未知**：协同消息是否升格为 Run / Event 需决策 |
| `wake_scheduled` | `schema.py:459`（`INTEGER NOT NULL DEFAULT 0`） | **无对应** | **G0-DEC-002**（唤醒调度与执行所有权） |
| `wake_round_id` | `schema.py:460` | **无对应**（`round_id` 在 Runtime 无列） | **G0-DEC-004** |
| `answered_at` | `schema.py:463` | **无对应** | **未知** |
| `expires_at` | `schema.py:464` | **无对应** | **G0-DEC-001**（超时语义） |
| `direction`（默认 `question`） | `schema.py:453` | **无对应** | **未知** |
| `in_reply_to` | `schema.py:455` | **无对应** | **未知** |
| 唤醒调度实现 | `collab/peer/scheduler.py:11/35/73` | **无对应** | **G0-DEC-002** |

### 3.7 审批

| 旧字段 | 证据 | 旧值 | Runtime 对应 | 判定 |
|---|---|---|---|---|
| `evoflow_tool_approvals.status` | `schema.py:1416`（默认 `pending`） | `pending` + 其他值（全集未穷举，见 U1） | `qagent_approvals.status` = `requested` / `granted` / `rejected`（`repository.py:334/387`） | **确定（目标存在）**，值域不同 → 需显式转换表；**`pending` → `requested` 属 G0-DEC-001**（审批语义） |
| `evoflow_tool_approval_grants.grant_all` | `schema.py:1402`（默认 0） | bool | **无对应**。Runtime 无"批量授权"概念 | **G0-DEC-001** |
| `signatures_json` | `schema.py:1403` | 字符串列表 | **无对应** | **未知** |
| `evoflow_tool_approval_audit.action` | `schema.py:1394` | 字符串 | **无对应**（审计不是 Runtime 职责） | **确定（不映射）** |
| `user_source`（默认 `ui`） | `schema.py:1395` | 字符串 | `qagent_approvals.decided_by:67`？ | **未知**：`decided_by` 是身份，`user_source` 是来源渠道，语义不同 |
| `evoflow_proactive_approvals.status` | `schema.py:1117`；取值见 `AG-G2-AGENT-002-A01` §3.4 | `pending` / `approved` / `rejected` / `timeout` / `escalated` | `requested` / `granted` / `rejected` | **G0-DEC-001**（`timeout` / `escalated` 无对应，与 Agent 域同） |
| `cancel_current_run` 后的审批终态 | `goal_service.py:1344`；`routers/collab.py:735`（`tool-approval/cancel`） | — | `request_cancel:324` 只转移 run 状态，**不触碰** `qagent_approvals` | **G0-DEC-001**（审批与 cancel 竞态）。Automation 域已把该子场景列入阻塞清单（execution-plan §2.6.4 第 1 行） |

### 3.8 取消 / 暂停 / 恢复 / 恢复会话

| 旧入口 | 证据 | Runtime 对应 | 判定 |
|---|---|---|---|
| `cancel_current_run` | `goal_service.py:1344`（`reason="user_stop"`） | `request_cancel:324` | **G0-DEC-001**：旧侧带 `reason`，Runtime 侧 `request_cancel` 的事件 payload 为 `{}`（`service.py:328`）→ 理由如何落库需决策 |
| `stop_goal` | `goal_service.py:1434`；`POST /api/goal/{session_id}/stop`（`goal.py:199`） | 同上 | **G0-DEC-001** |
| `pause_goal` / `resume_goal` | `goal_service.py:1369/1389` | **无对应**（Runtime 无 pause 状态） | **G0-DEC-001** |
| `end_goal` | `goal_service.py:1411` | **无对应**（`end` 是会话级，不是 run 级） | **未知** |
| `resume_run:337` | `service.py:337` | 旧侧无同名概念 | **G0-DEC-004**（恢复语义） |
| `recover_persisted_sessions` | `goal_service.py:1654` | `service.recover_incomplete_runs`（`background_startup.py:149-154` 调用） | **G0-DEC-004** |
| 终态防回退 | 本域无实现证据 | `TERMINAL_STATUSES`（`events.py:29`）；Automation 域已实现 stage rank + `_is_duplicate` | **确定（Runtime 能力已存在）** |

---

## 4. G0 依赖汇总（逐列，不得实现）

| 决策 | 本卡命中的映射项 |
|---|---|
| `G0-DEC-001`（审批与 cancel 竞态） | 3.1 `pending_feedback` / `feedback_timeout_at`；3.2 `planned` / `paused`；3.3 `plan_ready` / `awaiting_exec` / `paused`；3.5 事件类型；3.6 `expires_at`；3.7 **全部审批行**、`grant_all`；3.8 `cancel_current_run` / `stop_goal` / `pause_goal` / `resume_goal` |
| `G0-DEC-002`（执行 claim 与所有权、跨实例触发幂等） | 3.4 **全部三行**（`execution_authorized` / `authorized_at` / `authorized_by`）；3.6 `wake_scheduled`、`wake_round_id`、唤醒调度实现 |
| `G0-DEC-003`（Event v1、sequence、cursor、gap） | 3.5 **全部四行**（`event_json` / `sort_order` ↔ `sequence` / 读取出口 / 事件类型） |
| `G0-DEC-004`（Recovery Point / attempt / 重启恢复） | 3.1 `goal_revision` / `continuation_suppressed` / `current_step` / `error_count`；3.6 `wake_round_id`；3.8 `resume_run` / `recover_persisted_sessions` |

---

## 5. A03 是否可创建：**否**

| roadmap §5.1 门槛 | 本域现状 | 满足 |
|---|---|---|
| 有真实 HTTP / 页面 / worker / service 入口 | ✅ `/api/goal`（13）、`/api/collab`（7）、`peer/scheduler.py` | 是 |
| 已定位唯一业务事实源和既有写路径 | ✅ A01 §3/§4、本卡 §2 | 是 |
| **已找到可复用 Runtime / AgentScope 调用点** | ❌ **本域接点 = 0**（A01 §5） | **否** |
| 不依赖未冻结的 `G0-DEC-001～004` | ❌ §4 四项**全部**命中 | **否** |
| 修改范围 ≤ 3 既有生产文件 + 1 新测试文件 | ❌ 无既有接线可改，任何接入都是新增 | **否** |
| 有定向可运行的验收命令 | — | 无实现则无从谈 |

**额外阻塞（本域特有）**：本域有**三套粒度不同的状态机**（会话级 `GoalStatus`、任务级 `TaskStatus`、线程级 `CollabPhase`），
Runtime 是 Run 级。粒度对齐方案本身是契约级决策，**不属于任何 A0x 卡的授权范围**。

**结论**：`AG-G2-GOAL-003-A01`～`A05` **不得创建**。本卡可执行的部分已执行完毕（映射），实现侧维持 `Blocked by G0-DEC-001～004`。

---

## 6. 未知项清单（不得补猜）

| # | 未知项 | 为什么不能猜 |
|---|---|---|
| U1 | `evoflow_tool_approvals.status` 的完整取值集合 | `schema.py:1416` 只给默认值 `pending`；`routers/collab.py:405/735` 的写入值需另行穷举，本卡未展开 |
| U2 | 三套状态机的粒度对齐方案 | 会话 / 任务 / 线程 vs Run，属契约级决策 |
| U3 | `pending_feedback` 与 Runtime `waiting_approval` 是否同一语义 | 一个是"等人反馈"，一个是"等审批"，源码无等价声明 |
| U4 | `awaiting_exec` 与 `waiting_approval` 是否同一语义 | 同上 |
| U5 | `executing` 在 Runtime 应映射 `running` 还是 `executing` | Runtime 同时存在两值，旧侧只有一值 |
| U6 | `goal_summary` 与 `completion_outcome` 如何拆入 `result_payload` | 旧侧两字段，新侧一个 JSON |
| U7 | `last_error`（字符串）→ `error_payload`（结构化）的转换规则 | 旧侧无结构 |
| U8 | `cancel_current_run(reason=...)` 的理由如何落 Runtime | `request_cancel` 的事件 payload 为 `{}`（`service.py:328`） |
| U9 | `end_goal`（会话级）是否有 Runtime 等价动作 | Runtime 无会话概念 |
| U10 | 协同消息是否应升格为 Run / Event | 源码无任何线索 |

## 7. 本卡是否产生代码改动

**否**。只写了 `artifacts/acceptance/agentscope-runtime/AG-G2-GOAL-002-A01/20260915/` 下的
`mapping.md` 与 `acceptance-rg.txt`。未修改任何源代码、测试、schema、migration 或计划外文档；
未创建或删除 branch / worktree；未执行任何被任务包禁止的 git 操作。
