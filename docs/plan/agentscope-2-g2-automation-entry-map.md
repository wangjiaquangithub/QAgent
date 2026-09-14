# G2 Automation / Task Center 接入地图（AG-G2-AUTO-001 产出）

- 卡片：`G2-AUTO-001`（Automation 盘点）
- 基线：`origin/codex/agentscope-runtime-migration`（`b09adf4`）
- 分支：`codex/g2-automation-task-center`
- 性质：**只读盘点产物**。本文档不含任何代码改动，不提出实现方案，仅登记现存真实入口、状态写路径与状态出口，供 G2-AUTO-002～006 作为改造锚点。
- 行号对应本次盘点时的 HEAD（`b09adf4`），仅作定位用途；后续卡片若改动这些文件，需重新核对行号。

---

## 1. 用户可见入口（既有 API，必须保持兼容）

### 1.1 自动化规则 / 调度 API

`backend/app/gateway/routers/automation_scheduler.py:20` → `APIRouter(prefix="/api/automation")`

| 路由 | 行 | 语义 |
|---|---|---|
| `POST /api/automation/schedule/preview` | 287 | 解析 cron / schedule，返回摘要与下次触发 |
| `GET /api/automation/scheduler/status` | 305 | 调度器状态 |
| `POST /api/automation/scheduler/start` | 311 | 客户端启动调度器 |
| `POST /api/automation/scheduler/stop` | 326 | 客户端停止调度器 |
| `GET /api/automation/feishu-push-default` | 332 | 推送默认值 |
| `GET /api/automation/push-targets` | 361 | 推送目标列表 |
| `GET /api/automation/tasks` | 368 | 规则列表 |
| `GET /api/automation/tasks/{task_id}/history` | 373 | 规则运行历史 |
| `GET /api/automation/tasks/{task_id}` | 390 | 规则详情 |
| `POST /api/automation/tasks` | 402 | 创建规则 |
| `PUT /api/automation/tasks/{task_id}` | 529 | 更新规则 |
| `DELETE /api/automation/tasks/{task_id}` | 607 | 删除规则 |
| `POST /api/automation/tasks/{task_id}/pause` | 621 | 暂停（禁用） |
| `POST /api/automation/tasks/{task_id}/resume` | 634 | 恢复 |
| `POST /api/automation/tasks/{task_id}/run` | 647 | 手动立即触发 |

模块 docstring 声明：CRUD 与 `evopanel/scripts/dev-api.js` 对齐，Web（dev-api）与 Desktop（Gateway，经 `gatewayProxy`）共用同一后端实现 —— **这是"新 Runtime 不得要求用户换入口"的硬约束来源**。

### 1.2 Task Center API

`backend/app/gateway/routers/tasks.py:40` → `APIRouter(prefix="/api/tasks", dependencies=[Depends(require_premium)])`

| 路由 | 行 | 语义 |
|---|---|---|
| `GET /api/tasks` | 604 | 任务列表（含过滤） |
| `GET /api/tasks/queue/status` | 909 | **无人值守任务队列状态** |
| `POST /api/tasks/queue/tick` | 918 | 手动跑一次队列 tick（debug） |
| `POST /api/tasks/batch/stop` | 925 | 批量停止 |
| `GET /api/tasks/{task_id}` | 980 | 任务详情 |
| `POST /api/tasks` | 1025 | 创建任务 |
| `PUT /api/tasks/{task_id}` | 1354 | 更新任务 |
| `DELETE /api/tasks/{task_id}` | 1579 | 删除任务 |
| `POST /api/tasks/{task_id}/start` | 1596 | 开始规划并执行 |
| `POST /api/tasks/{task_id}/stop` | 1641 | 停止 |
| `POST /api/tasks/{task_id}/resume` | 1662 | 恢复 |
| `POST /api/tasks/{task_id}/restart` | 1739 | 重启（克隆为新任务，原任务归档） |
| `POST /api/tasks/{task_id}/cancel` | 1905 | 取消 |
| `POST /api/tasks/{task_id}/retry` | 2399 | 重试失败的子任务 |
| `GET /api/tasks/{task_id}/execution-history` | 2537 | 执行历史 |
| `GET /api/tasks/{task_id}/runtime` | 2607 | 运行时状态（含 agent 分配） |

### 1.3 状态出口（SSE / WebSocket / 诊断）

`backend/app/gateway/routers/events.py:21` → `APIRouter(prefix="/api/events")`

| 出口 | 行 | 类型 |
|---|---|---|
| `/api/events/tasks/{task_id}/stream` | 171 | SSE —— Task Center 主状态流 |
| `/api/events/tasks/{task_id}/stream-diagnostics` | 557 | SSE —— 流诊断 |
| `/api/events/threads/{thread_id}/panel-stream` | 358 | SSE —— 面板流 |
| `/api/events/threads/{thread_id}/stream-status` | 385 | SSE —— 流状态 |
| `/api/events/ws/threads/{thread_id}` | 344 | WebSocket |
| `/api/events/internal/broadcast` | 267 | 内部广播注入（非公开） |
| `/api/events/internal/inject-custom` | 285 | 内部流注入（非公开） |
| `/api/events/internal/inject-evf` | 319 | 内部流注入（非公开） |

任务生命周期事件类型定义在 `backend/app/gateway/events/task_events.py`：`TaskAuthorizedEvent`(12)、`TaskExecutionStartedEvent`(47)、`TaskExecutionFailedEvent`(59)、`TaskCancelEvent`(72)、`TaskCancelledEvent`(104)、`TaskResumeEvent`(116)。**这是 G2-AUTO-002 定义 Event 映射时唯一应当复用的既有事件词汇表。**

---

## 2. 调度器（两条独立循环，各自有开关）

### 2.1 自动化调度器

`backend/app/gateway/automation_runner.py`

| 符号 | 行 | 作用 |
|---|---|---|
| `automation_scheduler_enabled()` | 1509 | 开关 |
| `automation_tick()` | 1298 | 单次 tick：扫描到期规则并触发 |
| `run_automation_scheduler(stop)` | 1480 | 常驻循环 |
| `get_automation_scheduler_status()` | 472 | 状态查询（对应 `/scheduler/status`） |
| `cron_matches_now(expr)` | 498 | 匹配判定 |
| `_effective_cron(task)` | 504 | 规则生效 cron（含 keyword 归一） |

### 2.2 Task Center 无人值守队列调度器

`backend/app/gateway/task_queue_runner.py`

| 符号 | 行 | 作用 |
|---|---|---|
| `task_queue_enabled()` | 24 | 开关 |
| `task_queue_max_concurrent()` | 29 | 并发上限 |
| `task_queue_tick_seconds()` | 37 | tick 间隔 |
| `get_task_queue_status()` | 45 | 状态（对应 `/queue/status`） |
| `task_queue_tick()` | 70 | 单次 tick |
| `run_task_queue_scheduler(stop)` | 132 | 常驻循环 |

> 两者是**平行循环**，各自读配置、各自 tick。G2-AUTO-003 若接入统一执行入口，必须保留这两个开关的独立语义，否则灰度无法按租户收敛。

---

## 3. 三条真实执行路径（同一规则的三种落点）

判定集中在 `_run_one_task`（`automation_runner.py:827`）。路径分派逻辑：

```
automation_tick()                                   automation_runner.py:1298
  └─ _run_one_task(task_id, ...)                    automation_runner.py:827
       ├─ [A] 绑定已发布工作流（app_id 非空）
       │     _run_bound_app_for_automation()        automation_runner.py:766   (line 867 分派)
       │       └─ _stamp_automation_on_collab_task() automation_runner.py:723
       │            └─ evoflow.collab.app_runner.run_app()          collab/app_runner.py:843
       │                 └─ 产出 collab_task_id → dispatch_workflow_task_now()  collab/app_runner.py:49
       │                      → Task Center 主任务（run_mode=unattended），由队列驱动
       │
       ├─ [B] prompt-only 规则，显式走 Task Center（opt-in）
       │     _automation_via_task_center()          automation_runner.py:570
       │       └─ _enqueue_automation_as_unattended_task()  automation_runner.py:590
       │            └─ _kick_unattended_task()      automation_runner.py:685
       │                 └─ advance_unattended_task()  unattended_task_pipeline.py:834
       │
       └─ [C] prompt-only 规则，直连 LangGraph（默认）
             _run_one_task_direct_langgraph()       automation_runner.py:1087
               └─ _invoke_langgraph_for_automation() automation_runner.py:210
```

### 关键语义（易被误判，务必保留）

`_automation_via_task_center`（`automation_runner.py:570-589`）docstring 明确：

- **默认 `False`** —— prompt-only 规则默认直连 LangGraph `runs.wait`（路径 C）。
- 绑定工作流（`app_id`）**永不**走该分支，固定走 `run_app_workflow`（路径 A）。
- 仅在规则字段 `execution_mode=task_center`（或 `plan` / `unattended` / `task_center_plan`）或环境变量 `EVOFLOW_AUTOMATION_VIA_TASK=1` 时启用 Task Center 路径（路径 B）。
- 显式 `direct` / `direct_langgraph` / `langgraph` / `execute` / `runs_wait` 强制回退直连。

→ **结论：三条路径目前不共享同一执行入口，但共享同一业务状态（见 §4）。** 因此 G2-AUTO-003 的接入点应选在执行入口的收敛处，而不是三条路径各自改一遍。

---

## 4. 状态写入路径

### 4.1 规则定义（双写：文件为源，PostgreSQL 为镜像）

| 存储 | 位置 | 读 | 写 |
|---|---|---|---|
| TOML 文件（权威源） | `~/.evoflow/tasks/automations` | `_load_automation_tomls()` `automation_runner.py:550`、`load_automation_toml()` `:557` | `_rewrite_automation_toml()` `:1288` |
| PostgreSQL 镜像 | 表 `evoflow_automations` | `list_automations()` `automation_repositories.py:30`、`load_automation()` `:35` | `save_automation()` `:49`、`delete_automation()` `:111` |
| 历史遗留导入 | — | — | `_maybe_import_legacy_toml_automations()` `automation_runner.py:444` |

持久层：`backend/packages/harness/evoflow/persistence/automation_repositories.py`

### 4.2 运行记录（PostgreSQL）

表 `evoflow_automation_runs`：

- 写入：`append_automation_run(task_id, record)` `automation_repositories.py:198`，调用方共 3 处，均在 `automation_runner.py`（**这是 G2-AUTO-004 "切换自动化正式 PostgreSQL 写入" 的现有写点集合**）
- 读取：`list_automation_runs()` `:228`、`count_automation_runs()` `:247`
- 对外出口：`/api/automation/tasks/{task_id}/history`（`_read_history()` `automation_scheduler.py:281`，limit=500）与列表页 latest_run（`_list_all_automations()` `:209`）

### 4.3 归属 / 可见性

- 写：`set_automation_owner_scope()` `automation_repositories.py:120`
- 读：`get_automation_owner_scope()` `:157`
- 判定：`automation_visible_to_principal(is_admin, personal_scope, org_scope, principal)` `:173`
- 上下文解析：`evoflow.authz.context.resolve_request_authz` + `evoflow.authz.scope.personal_scope` / `org_scope`（`automation_scheduler.py:261`、`:209`）

### 4.4 Task Center / 无人值守任务状态

`backend/app/gateway/unattended_task_pipeline.py`

| 符号 | 行 | 作用 |
|---|---|---|
| `is_unattended_task(task)` | 68 | 无人值守判定 |
| `_save_task_row(project, index, task)` | 175 | **任务行写回** |
| `_patch_task(task_id, mutator)` | 181 | **任务行局部修改** |
| `_finalize_executing_task(task_id)` | 457 | 终态收敛 |
| `_mark_unattended_plan_failed(task_id, reason)` | 132 | 失败标记 |
| `_advance_unattended_task_impl(task_id)` | 599 | 推进主实现 |
| `advance_unattended_task(task_id)` | 834 | **对外推进入口** |
| `list_unattended_candidates()` | 841 | 候选（队列取活） |
| `list_unattended_in_progress()` | 871 | 在途 |
| `count_active_unattended_tasks()` | 917 | 活跃计数（并发槽） |

子任务状态判定：`_has_active_subtasks`(196)、`_has_in_flight_subtasks`(206)、`_all_subtasks_terminal`(214)、`_all_subtasks_success`(221)

LangGraph 线程准备：`_ensure_langgraph_thread(task_id, task)` `:398` —— **触碰 LangGraph 主链，本域不得修改**。

---

## 5. 幂等、重试、并发与恢复（G2-AUTO-002 / 004 直接依据）

| 机制 | 位置 | 说明 |
|---|---|---|
| 每任务推进锁 | `_advance_lock_for(task_id)` `unattended_task_pipeline.py:229` | 进程内 asyncio 锁，**非跨进程 / 非跨实例** |
| 重试次数上限 | `task_queue_max_retries()` `:46` | 配置驱动 |
| 重试退避序列 | `task_queue_retry_delays_seconds()` `:54` | 配置驱动 |
| 退避到期判定 | `_retry_due(task)` `:93` / `_schedule_retry(task)` `:100` | — |
| 规划触发陈旧判定 | `_plan_trigger_stale(task)` `:287` | 防止重复触发规划 |
| 规划触发 | `_trigger_unattended_plan_run()` `:294` / `_maybe_trigger_unattended_plan()` `:364` | — |
| 调度槽位键 | `_slot_key()` `automation_runner.py:565` | 并发去重相关 |
| LangGraph 运行信号量 | `_langgraph_run_semaphore()` `automation_runner.py:42` | 进程内限流 |
| 规划是否占用并发槽 | `_planning_consumes_concurrency_slot(task)` `:897` | — |

> ⚠️ **风险登记**：当前幂等与互斥原语（任务推进锁、信号量、槽位键）**全部是单进程内存态**。G2-AUTO-002 定义 claim / attempt / 幂等映射时，必须显式回答"多实例下同一触发窗口如何不重复创建 Run"，而这属于 Plan 尚未冻结的生产语义 —— 若无法在既有语义内闭环，应按任务规则**停止并上报**，不得自行引入 leader / lease / fencing。

### 既有对账 / 恢复组件（只读参考，不属本卡改动范围）

- `backend/app/gateway/run_status_reconcile.py`
- `backend/app/gateway/zombie_run_sweeper.py`
- `backend/app/gateway/routers/runs.py`

---

## 6. 与 G2-AUTO 后续卡片的对应关系

| 母任务 | 本地图提供的锚点 | 尚缺、留给后续卡 |
|---|---|---|
| G2-AUTO-002 映射 | §3 三路径、§4 状态写点、§5 幂等原语、§1.3 既有事件词汇表 | trigger→Run→attempt→claim→Event 的最小映射表；多实例幂等语义 |
| G2-AUTO-003 接入 | §2 两个独立调度开关、§3 执行入口收敛处（`_run_one_task` / `advance_unattended_task`） | Adapter 挂载点选择；不得改 `_ensure_langgraph_thread` / `_invoke_langgraph_for_automation` |
| G2-AUTO-004 正式写入 | §4.1 规则双写（TOML→PG 镜像）、§4.2 `append_automation_run` 三处写点 | 重启恢复路径；TOML 源与 PG 的权威切换策略 |
| G2-AUTO-005 测试对账 | §1 全部入口、§4 全部写点、§1.3 终态事件 | 定时 / 重入 / 并发 / 失败 / 禁用 / 时区 / 恢复 / 权限用例矩阵 |
| G2-AUTO-006 灰度 | §2.1 `automation_scheduler_enabled`、§2.2 `task_queue_enabled` | 租户级灰度开关、触发次数与 Run 数一致性对账、旧调度停写 |

---

## 7. 本卡范围声明

本卡**仅新增本文档**，未修改任何源码。未触碰：

- `backend/app/qagent_runtime/`
- `backend/migrations/`
- LangGraph 主链与 `/api/langgraph`（`routers/langgraph_proxy.py`、`gateway/lazy_langgraph.py` 等）
- Gateway 主干（`gateway/app.py`、`gateway/router_registry.py`、`gateway/background_startup.py`）
- Chat / Live Run 域
- 设备协议、lease / fencing
- 前端页面、依赖版本

盘点方法：受控 `rg`（限定 `backend/app/gateway`、`backend/evoflow` 直接命中目录）+ 精确 `sed` 读取；未做全仓遍历、未建立索引、未使用 CodeSynapse。
