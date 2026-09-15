# AG-G2-AGENT-001-A01 — 数字员工 / Agent 真实入口与事实源盘点

- 卡片：`AG-G2-AGENT-001-A01`（母任务 `G2-AGENT-001`）
- 日期 / attempt：2026-09-15 / `20260915`
- 起始 HEAD：`cf57d5f1363c4f5df9c8729fb5e4f67ef896f0a7`（分支 `codex/agentscope-runtime`）
- 验收命令（卡片原文）：
  ```
  rg -n "api/proactive|employees\.|start_proactive_runner|evoflow_proactive_(roles|initiatives|approvals)|ProactiveEngine|ExecutionBridge" \
     backend/packages/harness/evoflow/proactive backend/packages/harness/evoflow/admin \
     backend/app/gateway/router_registry.py backend/app/gateway/background_startup.py
  ```
  原始输出：`acceptance-rg.txt`（130 行，与本文件同目录）。零 Runtime 接点核对：`harness-runtime-refs.txt`。
- 本卡**只读**：未修改 `evoflow/proactive/**`、`admin/employees.py`、`goal_service.py` 或任何 backend 代码、测试、schema、migration、配置。

---

## 1. 真实用户入口（HTTP）

### 1.1 `/api/proactive`（执行入口，员工值班与协同面）

- 挂载：`backend/app/gateway/router_registry.py:126-128` —— `from evoflow.proactive.router import router as proactive_router` 后
  `app.include_router(proactive_router, dependencies=[Depends(require_premium)])`。
- 定义：`backend/packages/harness/evoflow/proactive/router.py:30` —— `APIRouter(prefix="/api/proactive", tags=["proactive"])`。
- 该 router 声明 **43 个端点**（`rg '^@router\.(get|post|put|delete|patch)\('`），按职责分四组：

| 组 | 端点（行号取 `router.py`） | 说明 |
|---|---|---|
| 岗位（角色）生命周期 | `GET /roles:250`、`GET /roles/{agent_code}:327`、`POST /roles:355`、`PUT /roles/{agent_code}:483`、`DELETE /roles/{agent_code}:643`、`PUT /roles/{agent_code}/archive:961`、`POST /roles/archive-legacy:991`、`POST /roles/check-overlap:932` | 对应事实源 `evoflow_proactive_roles` 的 CRUD |
| 值班控制 | `POST /roles/{agent_code}/heartbeat:742`、`PUT /roles/{agent_code}/pause:2210`、`POST /roles/{agent_code}/stop:2247`、`PUT /roles/{agent_code}/resume:2279`、`POST /enable:2179`、`POST /disable:2190`、`GET /status:1996` | 心跳与启停；`pause`/`resume`/`stop` 与 `admin/employees.py` 同名动作对应 |
| 派工与工作台 | `POST /roles/{agent_code}/dispatch:1142`、`GET /roles/{agent_code}/busy:1235`、`GET /roles/{agent_code}/work-board:1321`、`GET /roles/{agent_code}/performance:1026`、`GET /roles/{agent_code}/growth:1047`、`GET /roles/{agent_code}/cost:2002`、`GET /dashboard:2032`、`POST /events:1100`、`POST /migrate-work-items:1433` | 派工是员工"接活"的入口 |
| 审批与协同视图 | `GET /initiatives:1302`、`GET /initiatives/{initiative_id}:1449`、`GET /approvals:1467`、`POST /approval/{item_id}:1568`、`POST /approval/callback:1812`、`POST /approvals/{approval_id}/push-feishu:875`、`POST /debug/push-test-approval:817`、`GET /push-log:1788` | 审批决定入口（飞书回调 + 桌面） |
| 记忆 / 会话 / 飞书绑定 | `GET /memory/{agent_code}:1863`、`GET /sessions/{agent_code}/conversations:1885`、`GET /sessions/{agent_code}/messages:1914`、`GET /org-tree:268`、`GET /roles/{agent_code}/feishu/binding:730`、`DELETE /roles/{agent_code}/feishu/binding:718`、`POST /roles/{agent_code}/feishu/registration/{session_id}/apply:681` | 只读视图 + 渠道绑定 |

- 该 router 直接构造旧引擎：`router.py:1502`、`:1767` → `from evoflow.proactive.engine import ProactiveEngine` 并 `engine = ProactiveEngine()`。

### 1.2 `/api/platform`（执行入口，动作注册表投影）

- 挂载与定义：`backend/app/gateway/routers/platform.py:19` —— `APIRouter(prefix="/api/platform", tags=["platform"])`；
  端点 `GET /catalog:39`、`POST "":46`、`POST "/":47`；请求体 `PlatformInvokeBody{action, args|args_json, domain, confirm}`（`platform.py:22-36`）；
  鉴权 `require_org_admin`（`platform.py:13` 导入）。
- 员工动作注册在 `backend/packages/harness/evoflow/admin/platform_actions.py:349-368`：
  `employees.list`、`employees.get`、`employees.hire`、`employees.update`、`employees.pause`、`employees.resume`、`employees.stop`、
  `employees.archive`、`employees.worklog`、`employees.trail`。
  写类动作需要 `confirm=true`（`platform.py:33-36`）。
- 处理函数映射：`backend/packages/harness/evoflow/admin/platform_handlers.py:747-842`（`H.employees_*`）。
- 实现在 `backend/packages/harness/evoflow/admin/employees.py`，本卡逐项核实行号（与卡片登记一致）：

| 动作 | 实现 |
|---|---|
| `employees.hire` | `employees.py:441 hire()` |
| `employees.update` | `employees.py:572 update_role()` |
| `employees.pause` | `employees.py:706 pause_role()` |
| `employees.stop` | `employees.py:725 stop_role()` |
| `employees.resume` | `employees.py:754 resume_role()` |
| `employees.archive` | `employees.py:789 archive_role()` |
| `employees.worklog` | `employees.py:961 worklog()` |
| `employees.trail` | `employees.py:1142 round_trail()` |
| （内部）派工 | `employees.py:1245 dispatch()` |
| （内部）唤醒 | `employees.py:1402 wake()` |

- `employees.py:155-160` 显示该模块通过 HTTP 回打 `/api/proactive`（把 LangGraph base URL 改写为 proactive base URL），
  即 `/api/platform` 与 `/api/proactive` 是**同一事实源的两个投影**，不是两套状态。

### 1.3 后台心跳循环（无 HTTP 入口的自动入口）

- `backend/app/gateway/background_startup.py:1028-1033`：`if is_premium_active(): start_proactive_runner()`（异常仅告警，非致命）。
- `start_proactive_runner` 定义在 `backend/packages/harness/evoflow/proactive/runner.py:3262`；
  `ProactiveRunner` 类在 `runner.py:410`，`start:510`、`enable:534`、`stop:553`、`is_running:588`、`last_tick_info:592`。
- 这是"员工自动值班"的唯一自动触发源，**不经 HTTP**，因此 A05 式的纯 HTTP 验收覆盖不到它。

### 1.4 配置入口（**不是**执行入口，必须区分）

- `backend/app/gateway/routers/agents.py`：`list_agents:502`、`create_agent_endpoint:739`、`update_agent:848`、`delete_agent:1137`
  —— 这是"Agent 角色配置"CRUD（`evoflow_agents`），**不触发值班、不产生 initiative、不派工**。
- 本卡结论：**Agent 域存在两个语义不同的对象**——"配置里的 Agent"（`agents.py` + `evoflow_agents`）
  与"值班员工 / 数字员工"（`/api/proactive` + `/api/platform employees.*` + `evoflow_proactive_roles`）。
  A02 做字段映射时必须按后者（值班员工）为主线，前者的 Runtime 迁移语义不在本批范围内。

---

## 2. 旧事实源（SQLite，`backend/packages/harness/evoflow/persistence/schema.py`）

本卡逐项核实行号（与卡片登记一致）：

| 表 | schema 行 | 关键列（本卡核实） |
|---|---|---|
| `evoflow_agent_runtime` | `:162` | `current_task_id:166`、`last_heartbeat:167`（+ `status`、`progress`） |
| `evoflow_agents` | `:174` | Agent 配置本体 |
| `evoflow_collab_tasks` | `:553` | 工作项落点（主任务 / 子任务），索引 `idx_evo_collab_tasks_main:1699` |
| `evoflow_proactive_approvals` | `:1117` | `round_id:1136`、`FOREIGN KEY(initiative_id) → evoflow_proactive_initiatives(id):1130`；索引 `:1875`、`:1878` |
| `evoflow_proactive_initiatives` | `:1147` | `approval_id:1158`、`execution_result:1163`、`round_id:1165`、`goal:1165`、`outcome:1165`、`config_autonomy_level:1165` |
| `evoflow_proactive_roles` | `:1175` | `heartbeat_rrule:1180`（默认 `FREQ=HOURLY;INTERVAL=2`）、`last_heartbeat_at:1182`、`next_heartbeat_at:1183`、`position_code`、`reports_to`；索引 `:1889`、`:1892` |

补充事实（本卡核实，供 A02 使用）：

- `evoflow_proactive_roles` 是 initiative 的父表：`evoflow_proactive_initiatives` 的
  `FOREIGN KEY (role_agent_code) REFERENCES evoflow_proactive_roles(agent_code)`（`schema.py:1166`）。
- 审批与 initiative 一对一挂钩：`evoflow_proactive_approvals.initiative_id → initiatives.id`（`schema.py:1130`）。
- `schema.py:2135` 把 `evoflow_proactive_initiatives` 列入某处表清单（同一批表管理逻辑内），说明它与 roles/approvals 同属一组。

### 2.1 状态枚举（权威定义，A02 必须以此为准）

卡片在 `AG-G2-AGENT-002-A01` 的映射范围里把角色状态写作 `active/paused/stopped/archived`。**源码与之不符**，
本卡以 `backend/packages/harness/evoflow/proactive/models.py` 的权威定义为准：

| 枚举 | 定义位置 | 取值 |
|---|---|---|
| `ROLE_STATUSES`（角色 / 员工岗位状态） | `models.py:267` | `active`、`paused`、`archived`、`draft` —— **没有 `stopped`，有 `draft`** |
| 校验 / 归一化 | `models.py:270 normalize_role_status()`（非法值抛 `ValueError`，默认 `active`） | — |
| `ProactiveRole.status` 字段声明 | `models.py:294`（注释 `active / paused / archived / draft`，与 `ROLE_STATUSES` 一致） | — |
| `InitiativeStatus`（initiative 生命周期） | `models.py:45-56` | `proposed`、`pending_approval`、`approved`、`rejected`、`executing`、`completed`、`failed`、`timeout_rejected`、`skipped` |
| `ApprovalStatus`（人工决定） | `models.py:59-66` | `pending`、`approved`、`rejected`、`timeout`、`escalated` |

**"停止"不是独立状态**：`models.py:158-163` 的注释明确 `stop_role` 的语义是
"skip and set role status to `paused`"，并与"请假式 `status=paused`"区分（stop 保留在岗 badge 但阻止自动巡查）。
因此 `employees.stop`（`admin/employees.py:725`）与 `PUT /api/proactive/roles/{agent_code}/stop`（`router.py:2247`）
落到的状态值都是 `paused` —— A02 必须按"两个入口、一个状态值"登记，不得假设存在 `stopped`。

---

## 3. 写路径（谁写哪张表）

| 事实源 | 写点（本卡核实的符号） |
|---|---|
| `evoflow_proactive_roles` | `admin/employees.py`：`hire:441`、`update_role:572`、`pause_role:706`、`stop_role:725`、`resume_role:754`、`archive_role:789`；以及 `proactive/router.py` 的 `POST /roles:355`、`PUT /roles/{agent_code}:483`、`DELETE /roles/{agent_code}:643`、`PUT .../archive:961` |
| `evoflow_proactive_initiatives` | `proactive/engine.py`：`_build_initiative:1149`（构造）、`think:173`（一轮思考产出 initiative）、`execute_initiative:204`（执行已批准 initiative） |
| `evoflow_proactive_approvals` | `proactive/decision_gate.py`：`DecisionGate:55`、`request_approval:105`、`request_approval_for_task:148`、`process_decision:212`、`check_timeouts:457` |
| 工作项 → `evoflow_collab_tasks` | `proactive/work_items.py`：`create_role_work_item:274`、`load_work_item_task:382`、`list_role_tasks_for_round:404`、`list_pending_work_items_for_round:456`、`list_role_open_tasks:501`、`list_role_recent_tasks:587`、`task_needs_approval:848` |
| `evoflow_agent_runtime` | `backend/app/gateway/routers/agents.py`（配置 CRUD 侧）+ 运行时心跳写入（`last_heartbeat`） |

---

## 4. 旧执行链（无 Runtime 接点）

卡片登记的三段式在源码中成立，本卡核实如下：

```
ProactiveEngine.think                       engine.py:173   （think_mode = agent_loop | prompt_only）
  ├─ _run_agent_loop                        engine.py:240
  ├─ _run_langgraph_agent                   engine.py:591
  └─ _run_prompt_only                       engine.py:933
        ↓ 产出 / 执行 initiative
ProactiveEngine.execute_initiative          engine.py:204
        ↓
ExecutionBridge                             execution_bridge.py:28   （execute:57 → _run_langgraph:110）
        ↓ 路由规则（execution_bridge.py:4-6 模块 docstring 原文）
  code_change / optimization -> LangGraph lead_agent run
  task_delegation            -> supervisor tool delegation
  analysis / report / alert  -> direct LangGraph run (read-only)
        ↓ 审批
DecisionGate                                decision_gate.py:55
        ↓ 工作项落地
create_role_work_item                       work_items.py:274  → evoflow_collab_tasks
```

- `ExecutionBridge` 的默认 assistant 为 `DEFAULT_ASSISTANT_ID = os.getenv("EVOFLOW_PROACTIVE_ASSISTANT_ID", "lead_agent")`（`execution_bridge.py:24`）；
  `execution_bridge.py:213` 注释明确 "Graph id is always lead_agent; bind employee via agent_name + context (not configurable)"。
- 因此本域执行**全部**经由 LangGraph / supervisor，不经过 QAgent Runtime。

---

## 5. Runtime 接点：**0**（显式结论）

卡片要求的"可为空但须显式写明为 0"：

```
$ rg -n "qagent_runtime|agentscope" backend/packages/harness | wc -l
0
```

- `backend/packages/harness` 全目录对 `qagent_runtime` / `agentscope` 的引用数为 **0**。
- 因此本域（数字员工 / Agent）现存 Runtime 接点 = **0**：没有任何 `create_run` / `start_run` / `request_cancel` / 事件回写调用，
  也没有 Runtime bridge、opt-in 开关或 linkage 写入。
- 与 Automation 域的差别必须写清：Automation 有 3 处既有接线（`unattended_task_pipeline.py:606/621/797`、
  `task_queue_runner.py`、`routers/tasks.py:1919`），本域**一处都没有**。
- 与 Apps / Chat 的差别：那两域已有"真实入口 → Runtime → AgentScope"首闭环；本域没有。

---

## 6. A02 是否有证据（结论：**有，但映射必须只做只读对照**）

`AG-G2-AGENT-002-A01` 需要的输入本卡已全部定位，因此 A02 **可以**实例化：

| A02 需要的输入 | 本卡是否已提供 |
|---|---|
| 真实入口 | ✅ §1（`/api/proactive` 43 端点 + `/api/platform employees.*` 10 动作 + 后台心跳） |
| 旧事实源与关键字段 | ✅ §2（6 张表 + 逐列行号） |
| 写点 | ✅ §3（按表的写点符号） |
| 旧执行链 | ✅ §4（engine → bridge → gate → work_items） |
| Runtime 公开契约 | ✅ 已在 execution-plan §2.6.2 的 `AG-G2-AGENT-002-A01` 条目登记（`service.py` 的 `create_run:70`、`start_run:85`、`get_run_status:301`、`stream_events:304`、`request_cancel:324`、`resume_run:337`、`get_result:374`；`events.py:13 EventType`、`:29 TERMINAL_STATUSES`） |
| Runtime 接点现状 | ✅ = 0（§5） |

**A02 的边界（必须遵守）**：
- 只做字段级对照，不实现接入。因为本域 Runtime 接点为 0，"接入"意味着新增 bridge、新增开关、新增 linkage 语义 ——
  这些都会触及状态机与公共契约，**不属于 A01/A02 授权范围**。
- 逐列必须标 `Blocked` 的四类：审批与 cancel 竞态 → `G0-DEC-001`；心跳调度与多实例"同一触发窗口只创建一个 Run"
  的执行所有权 → `G0-DEC-002`；事件字段 / sequence / cursor / gap → `G0-DEC-003`；round 续跑、
  `recover_persisted_sessions` 与 attempt → `G0-DEC-004`。
- 已登记缺口（只登记，不改）：`backend/app/qagent_runtime/contract.py:32 RuntimeContract.create_run` **未声明 `org_id`**，
  而 `service.create_run` 接受该关键字 —— 与 Automation 域 P1 缺口同源。

---

## 7. 停止条件检查

| 卡片停止条件 | 本卡是否触发 |
|---|---|
| 只能靠臆造入口 / 字段 / Runtime 语义 | 否。§1~§5 全部为 `文件:行` 可核实事实；§5 的 0 接点由 `rg` 计数直接证明 |
| 需要生产代码或 schema 变更 | 否。未修改任何文件 |
| 发现与正在进行的 Chat 任务冲突 | 否。本域与 Chat 域无共享写路径（Chat 走 `chat_runtime_entry.py`，本域走 `proactive/*`） |

## 8. 未覆盖 / 需下游卡注意

| # | 事项 | 说明 |
|---|---|---|
| U1 | ~~`evoflow_proactive_roles.status` 的取值集合~~ | **已在本卡核实并修正**，见 §2.1：权威值为 `active/paused/archived/draft`（`models.py:267`），卡片登记的 `active/paused/stopped/archived` 不准确。A02 直接引用 §2.1，不必重查 |
| U2 | `evoflow_agents` 与 `evoflow_proactive_roles` 的关联方式 | 本卡只确认两表独立存在、且 `employees.hire` 以 `agent_code` 为键；A02 需核实 `agent_code` 到 `evoflow_agents` 的对应关系 |
| U3 | 后台心跳的触发窗口语义 | `ProactiveRunner.start:510` 的调度细节未展开；属 `G0-DEC-002` |
| U4 | `/api/platform` 与 `/api/proactive` 的写冲突面 | 两处都写 `evoflow_proactive_roles`（§3）；并发语义未验证，A02 只登记 |
| U5 | 飞书回调路径 | `router.py:1812 POST /approval/callback` 与 `decision_gate._push_feishu:586` 构成外部副作用边界；属 Knowledge / Channels 域（`AG-G2-KNOW-001-A01`）范围，本卡不展开 |
