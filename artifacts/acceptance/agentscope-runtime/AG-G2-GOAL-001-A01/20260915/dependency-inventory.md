# AG-G2-GOAL-001-A01 — Goal / 协同真实入口与 G0 依赖盘点

- 卡片：`AG-G2-GOAL-001-A01`（母任务 `G2-GOAL-001`，状态 `Blocked by G0-DEC-001～004`，仅只读盘点 / 依赖确认）
- 日期 / attempt：2026-09-15 / `20260915`
- 起始 HEAD：`ad08d20a2d667f1063b6dc83e8f78ccc3d1a034e`（分支 `codex/agentscope-runtime`）
- 验收命令（卡片原文，范围限定在已定位目录内）：
  ```
  rg -n "evoflow_collab_|evoflow_(proactive|tool)_approvals|approval|claim|cursor|recovery|attempt" \
     backend/app/gateway/routers/goal.py backend/app/gateway/routers/collab.py \
     backend/app/channels/services/goal_service.py backend/packages/harness/evoflow/collab \
     backend/packages/harness/evoflow/persistence/schema.py
  ```
  原始输出：`acceptance-rg.txt`（173 行，与本文件同目录）。
- 本卡**只读**：未实现任务 / 审批 / 协同的 Runtime 接入，未改 schema、migration、Runtime、Gateway、前端或 Chat；
  未纳入 HA、备份恢复、灾备、压测、历史迁移。**未设计任何替代语义**。

---

## 1. 结论先行

| 问题 | 结论 |
|---|---|
| 入口、事实源、写点是否全部可证明？ | **是**。§2~§4 每项都有 `文件:行` 证据 |
| 本域 Runtime 接点是多少？ | **0**。`backend/packages/harness` 与 `backend/app/channels/services/goal_service.py` 对 `qagent_runtime` / `agentscope` 的引用数**均为 0** |
| 是否依赖四项未冻结 G0 决策？ | **四项全部依赖**，且不是"某一两个字段"级别 —— 取消、审批、执行所有权、事件协议、恢复各自命中一项。见 §6 |
| 可继续还是 Blocked？ | **Blocked**（可继续的部分仅限只读证据；任何实现都先要 G0 冻结） |
| 是否有可在 G0 冻结前合法推进的下一步？ | 只有 `AG-G2-GOAL-002-A01`（字段级映射，只登记不实现）。**A03～A05 不得实例化** |

---

## 2. 真实用户入口（HTTP）

### 2.1 `/api/goal`（13 个端点）

- 定义：`backend/app/gateway/routers/goal.py:13` —— `APIRouter(prefix="/api/goal", tags=["goal"])`。
- 鉴权：`require_org_admin`、`require_session_visible`（`goal.py:10` 导入）。
- 服务注入：`get_goal_service()`（`goal.py:17`），注释明确"与 automation_runner 同源：从环境变量读 LangGraph URL"。

| 方法 | 路径 | 装饰器行 | 用途 |
|---|---|---|---|
| POST | `/start` | `:154` | 启动托管目标 |
| POST | `/notify-feishu-completion` | `:187` | 完成推送 |
| POST | `/{session_id}/stop` | `:199` | 停止（`stop_goal`） |
| POST | `/{session_id}/pause` | `:225` | 暂停（`pause_goal`） |
| POST | `/{session_id}/resume` | `:246` | 恢复（`resume_goal`） |
| POST | `/{session_id}/end` | `:267` | 结束（`end_goal`） |
| POST | `/{session_id}/after-chat-turn` | `:288` | 每轮对话后推进 |
| GET | `/settings-by-key/{session_key}` | `:334` | 读设置 |
| PUT | `/settings-by-key/{session_key}` | `:351` | 写设置 |
| GET | `/by-key/{session_key}` | `:377` | 按 key 读会话 |
| GET | `/{session_id}/status` | `:400` | 读状态 |
| GET | `/{session_id}/history` | `:426` | 读历史 |
| POST | `/{session_id}/feedback` | `:451` | 提交反馈（`submit_feedback`） |

> 卡片登记的 `POST /start:160` 等行号是**函数定义行**；装饰器在函数定义前 5 行（`:154`）。两者指向同一端点，本卡以装饰器行为准并注明差异。

### 2.2 `/api/collab`（7 个端点）

- 定义：`backend/app/gateway/routers/collab.py:23` —— `APIRouter(prefix="/api/collab")`。

| 方法 | 路径 | 行 | 用途 |
|---|---|---|---|
| GET | `/threads/{thread_id}/tool-approval/pending` | `:385` | 待审批工具调用 |
| POST | `/threads/{thread_id}/tool-approval` | `:405` | 批准 / 拒绝 |
| POST | `/threads/{thread_id}/tool-approval/cancel` | `:735` | 取消审批 |
| GET | `/threads/{thread_id}` | `:776` | 协同状态（`ThreadCollabStateResponse`） |
| GET | `/threads/{thread_id}/mission-analysis` | `:807` | 任务分析 |
| PUT | `/threads/{thread_id}` | `:825` | 更新协同状态 |
| GET | `/tasks/{main_task_id}/subtasks/{subtask_id}/history` | `:880` | 子任务历史 |

### 2.3 协同唤醒调度（无 HTTP 入口）

`backend/packages/harness/evoflow/collab/peer/scheduler.py`：
`schedule_peer_wake:11`、`enqueue_peer_wake_if_busy:35`、`drain_peer_wake_queue:73`。
与 Agent 域的心跳同类 —— **不经 HTTP 的自动触发源**，纯 HTTP 验收覆盖不到。

---

## 3. 旧事实源（SQLite）

行号取 `backend/packages/harness/evoflow/persistence/schema.py`（本卡核实，与卡片登记一致）：

| 表 | schema 行 | 说明 |
|---|---|---|
| `evoflow_goal_sessions` | `:676` | Goal 会话主表 |
| `evoflow_collab_tasks` | `:553` | 主任务 / 协同任务 |
| `evoflow_collab_subtasks` | `:503` | 子任务 |
| `evoflow_collab_peer_messages` | `:447` | 协同消息 |
| `evoflow_collab_task_execution_history` | `:544` | 任务执行历史 |
| `evoflow_proactive_approvals` | `:1117` | 员工侧审批（与 Agent 域共用） |
| `evoflow_tool_approvals` | `:1407` | 工具调用审批 |
| `evoflow_tool_approval_grants` | `:1399` | 审批授权记录 |
| `evoflow_tool_approval_audit` | `:1387` | 审批审计 |

### 3.1 `evoflow_goal_sessions` 关键列（`schema.py:676-713`）

`session_key`（PK）、`user_id`、`goal_session_id`、`prompt`、`max_steps`（默认 8）、`step_delay_ms`（1200）、
`retry_limit`（2）、`auto_stop_minutes`（0）、`persona_style`、`initiative`（70）、`emotional_intelligence`、
`feishu_push_on_complete`、`continuous_learning`、`use_evolution_skill`、
**`goal_status`（默认 `active`，`:691`）**、**`goal_revision`（默认 1，`:692`）**、
**`continuation_suppressed`（默认 0，`:693`）**、**`status`（默认 `idle`，`:694`）**、
**`step_count`（默认 0，`:695`）**、`enabled`、`last_run_at`、**`last_run_id`（`:699`）**、`last_error`、
**`pending_feedback`（默认 0，`:700`）**、`feedback_prompt`、**`error_count`（`:702`）**、`ended_at`、
`start_time`、`locked_chat_model`、`channel_type`（默认 `web`）、`system_prompt`、
`compaction_summary`、`goal_summary`、**`completion_outcome`（`:709`）**、`created_at`、`updated_at`。

### 3.2 `evoflow_tool_approvals` 关键列（`schema.py:1407-1420`）

`id`（自增 PK）、`session_key`、`thread_id`、`tool_call_id`、`tool_name`、`args_json`、`summary`、
`signature`、**`status`（默认 `pending`，`:1416`）**、`created_at`、`updated_at`；
唯一约束 `UNIQUE(session_key, tool_call_id)`（`:1419`）。

### 3.3 旧状态机（三套并存，必须区分）

| 枚举 | 位置 | 取值 |
|---|---|---|
| `GoalStatus`（托管运行状态） | `backend/app/channels/models/goal.py:9-16` | `idle`、`running`、`waiting`、`paused`、`error` |
| `TaskStatus`（协同任务） | `backend/packages/harness/evoflow/collab/models.py:59-71` | `inbox`、`pending`、`planning`、`planned`、`executing`、`paused`、`reviewed`（legacy，写入时映射为 `completed`）、`completed`、`failed`、`cancelled` |
| `CollabPhase`（每线程协同阶段） | `collab/models.py:23-35` | `idle`、`req_confirm`、`planning`、`plan_ready`、`awaiting_exec`、`executing`、`verifying`、`reflecting`、`paused`、`done` |

状态转移守卫：`collab/state_transitions.py` 的 `can_transition:102`、`validate_transition:116`、`get_allowed_transitions:130`。

**这是本域与 Automation 域最大的结构差异**：Automation 只有一套 `TaskStatus`；本域**三套状态机同时存在**
（Goal 会话状态、协同任务状态、协同阶段），三者之间没有单一映射表，A02 必须逐对声明关系，不得合并。

---

## 4. 写路径

| 事实源 | 写点（`backend/app/channels/services/goal_service.py`） |
|---|---|
| `evoflow_goal_sessions` | `_sync_session_from_goal_state:537`、`_persist_session_snapshot:1547`、`recover_persisted_sessions:1654` |
| Goal 执行 | `_run_goal_graph:608`、`ensure_goal_stream_loop:808` |
| 取消 / 暂停 / 恢复 / 结束 / 停止 | `cancel_current_run:1344`、`pause_goal:1369`、`resume_goal:1389`、`end_goal:1411`、`stop_goal:1434` |
| 用户干预 / 反馈 | `apply_user_steering:1923`、`submit_feedback:1965` |
| 协同任务与子任务 | `evoflow_collab_tasks` / `evoflow_collab_subtasks`，经 `collab/storage.py`（`find_main_task:435`、`get_project_storage:956`） |
| 协同消息 | `evoflow_collab_peer_messages`，经 `collab/peer/scheduler.py:11/35/73` |
| 工具审批 | `evoflow_tool_approvals`（`status` 默认 `pending`），经 `routers/collab.py:405/735` |

---

## 5. Runtime 接点：**0**（显式结论）

```
$ rg -n "qagent_runtime|agentscope" backend/packages/harness | wc -l
0
$ rg -n "qagent_runtime|agentscope" backend/app/channels/services/goal_service.py | wc -l
0
```

- 旧执行链为 `LangGraphClient` + `_run_goal_graph:608`（`goal.py:17` 的 `get_goal_service` docstring 也写明"从环境变量读 LangGraph URL"）。
- 本域**没有**任何 `create_run` / `start_run` / `request_cancel` / 事件回写 / linkage / opt-in 开关。
- 与 Automation 域的差别必须写清：Automation 有 3 处既有接线；本域**一处都没有**。

---

## 6. G0 依赖逐项确认（核心）

卡片要求"逐项确认是否依赖四项未冻结 G0 决策"。结论：**四项全部依赖**，逐项证据如下。

### 6.1 `G0-DEC-001`（审批与 cancel 竞态）—— **依赖**

| 事实 | 证据 |
|---|---|
| 本域有独立的工具审批状态机 | `evoflow_tool_approvals.status` 默认 `pending`（`schema.py:1416`）；`routers/collab.py:405` 批准 / 拒绝、`:735` 取消审批 |
| 审批与取消是两个独立入口，可并发 | `POST /api/collab/threads/{thread_id}/tool-approval`（`:405`）与 `POST .../tool-approval/cancel`（`:735`）无共享锁证据 |
| Goal 侧取消与审批无耦合处理 | `cancel_current_run:1344` 只处理 run，不触碰审批表 |
| 员工侧审批同构 | `evoflow_proactive_approvals`（`schema.py:1117`），已有 `ApprovalStatus` 的 `timeout` / `escalated` 值（Agent 域 A02 §3.4）在 Runtime 无对应 |

**未冻结时的允许动作**：只登记"审批与 cancel 会并发、当前无仲裁"这一事实；**不得实现**任何仲裁 / 优先级 / 回滚语义。

### 6.2 `G0-DEC-002`（执行 claim 与所有权、跨实例触发幂等）—— **依赖**

| 事实 | 证据 |
|---|---|
| 协同唤醒有排队语义但无跨实例所有权 | `peer/scheduler.py` 的 `schedule_peer_wake:11`、`enqueue_peer_wake_if_busy:35`、`drain_peer_wake_queue:73` |
| Goal 会话有 `last_run_id` 但无 claim | `evoflow_goal_sessions.last_run_id`（`schema.py:699`）、`step_count`（`:695`） |
| 验收命令命中的 `claim` / `attempt` 关键词 | 见 `acceptance-rg.txt`（本域命中的多为注释与旧语义，非 Runtime claim） |

**未冻结时的允许动作**：只读盘点现有排队 / 去重线索；**不得实现**持久化 claim / lease / fencing。

### 6.3 `G0-DEC-003`（Event v1、sequence、cursor、gap 的业务映射）—— **依赖**

| 事实 | 证据 |
|---|---|
| 本域有流式推进 | `ensure_goal_stream_loop:808`、`POST /{session_id}/after-chat-turn`（`goal.py:288`） |
| 本域有历史读取出口 | `GET /{session_id}/history`（`goal.py:426`）、`GET /api/collab/tasks/{main_task_id}/subtasks/{subtask_id}/history`（`collab.py:880`）、`evoflow_collab_task_execution_history`（`schema.py:544`） |
| Runtime 侧事件契约已存在 | `events.py:13 EventType`（12 个）、`:29 TERMINAL_STATUSES`、`:30 INCOMPLETE_STATUSES`、`event_dict:37` |
| 两侧 sequence 语义未对齐 | 旧侧无 `sequence` 列证据；Runtime 侧 `qagent_run_events.sequence` 有唯一约束（`models.py:53`） |

**未冻结时的允许动作**：只记录现有事件证据；**不得实现**协议或补偿语义；不得引入流式帧 / cursor / gap。

### 6.4 `G0-DEC-004`（Recovery Point / attempt 与重启恢复）—— **依赖**

| 事实 | 证据 |
|---|---|
| 本域**已有**重启恢复实现 | `recover_persisted_sessions:1654` |
| 本域有 revision / 抑制续跑语义 | `goal_revision`（`schema.py:692`）、`continuation_suppressed`（`:693`） |
| Runtime 侧恢复契约已存在 | `qagent_recovery_points`（`models.py:72-82`）、`service.recover_incomplete_runs`（`background_startup.py:149-154` 调用） |
| 两侧 attempt 语义未对齐 | 旧侧 `step_count` / `error_count` / `goal_revision` 与 Runtime 的 `resume_count`（`models.py:26`）/ `execution_epoch`（`:29`）不是同一概念 |

**未冻结时的允许动作**：只记录恢复入口和缺口；**不得实现**恢复或历史迁移。

---

## 7. 结论：可继续 / Blocked

| 范围 | 判定 | 依据 |
|---|---|---|
| `AG-G2-GOAL-001-A01`（本卡） | **可继续且已完成** | §2~§5 全部为只读证据 |
| `AG-G2-GOAL-002-A01`（字段级映射） | **可继续** | 入口 / 事实源 / 写点 / 旧状态机已定位；该卡本身要求"未知项与 G0 依赖项必须显式标 `Blocked`，不得补猜" |
| `AG-G2-GOAL-003-A01`～`A05` | **Blocked，不得实例化** | 本域 Runtime 接点 = 0（不满足 roadmap §5.1 的"已存在的 Runtime 调用点"门槛）；且 §6 四项 G0 决策全部命中 |
| 任何绕过 G0 的实现方案 | **禁止** | 卡片停止条件："任何人要求猜测 G0 语义或创建绕过决策的实现卡" |

**最小解除条件**（供负责人决策，本卡不选方案）：

1. 冻结 `G0-DEC-001～004`（尤其 001 的审批 / cancel 竞态与 002 的执行所有权 —— 这两项直接决定本域能否在**不新增状态源**的前提下接入）；
2. 明确本域三套状态机（`GoalStatus` / `TaskStatus` / `CollabPhase`）与 Runtime 状态词汇的权威对应关系（A02 会逐对列出，但最终取舍需拍板）；
3. 若决定接入，需先决定是否新开一张"Goal 域 Runtime 接点"前置卡 —— 本域不存在可改的既有接线，任何接入都是**新增** bridge / 开关 / linkage 语义，超出本批授权。

## 8. 未覆盖 / 需下游卡注意

| # | 事项 | 说明 |
|---|---|---|
| U1 | `goal_status` 的完整取值集合 | `schema.py:691` 只给出默认值 `active`；全集需从 `app/channels/models/goal.py` 或服务层取，A02 必须核实后引用 |
| U2 | 三套状态机的相互关系 | `GoalStatus` / `TaskStatus` / `CollabPhase` 是否一一对应、是否有权威转换函数，本卡未穷举；A02 需逐对声明 |
| U3 | `evoflow_collab_tasks` 的 `TaskStatus` 与 Automation 域是否同一套 | 两处都用 `TaskStatus`，但 Automation 域走 `unattended_task_pipeline`，本域走 `collab/storage.py`；是否为同一枚举实例需核实 |
| U4 | 工具审批的 `grants` / `audit` 与 `approvals` 的关系 | `schema.py:1387/1399/1407` 三表关系未展开 |
| U5 | 飞书渠道 | `goal.py:8` 导入 `push_markdown_to_default_feishu_chat`；外部副作用边界属 Knowledge / Channels 域（`AG-G2-KNOW-001-A01`），本卡不展开 |
| U6 | `recover_persisted_sessions` 的触发点 | 本卡只定位函数（`goal_service.py:1654`），未核实其调用方与启动时序 |

## 9. 本卡是否产生代码改动

**否**。只写了 `artifacts/acceptance/agentscope-runtime/AG-G2-GOAL-001-A01/20260915/` 下的
`dependency-inventory.md` 与 `acceptance-rg.txt`。未修改任何源代码、测试、schema、migration 或计划外文档；
未创建或删除 branch / worktree；未执行任何被任务包禁止的 git 操作。
