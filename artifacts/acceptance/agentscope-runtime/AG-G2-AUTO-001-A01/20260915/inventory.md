# AG-G2-AUTO-001-A01 — Automation / Task Center 真实入口与旧状态机盘点

- 卡片：`AG-G2-AUTO-001-A01`（母任务 `G2-AUTO-001`，阶段 G2，Owner Domain Migration）
- 动作类型：只读盘点（不修改任何生产代码、测试、schema、migration、配置）
- 工作目录 / 分支：`/Users/wangjiaquan/project/QAgent-agentscope-runtime` / `codex/agentscope-runtime`
- 起始 HEAD：`5fc6e749be89d4b52c7cf9313de0159d1ca1e243`
- attempt：`20260915`
- 验收命令（原文，唯一首条命令）：
  ```bash
  rg -n "automation_scheduler|task_runtime_|TaskAuthorizedEvent|TaskExecutionStartedEvent|TaskExecutionFailedEvent|TaskCancelEvent|TaskCancelledEvent|TaskResumeEvent" backend/app/gateway
  ```
  原始输出：`rg-hits.txt`（同目录，128 行，exit=0）。

> 行号对应起始 HEAD `5fc6e74`，仅用于定位；后续改动这些文件需重新核对。

---

## 1. 真实入口（既有 API，必须保持兼容）

| 入口 | 路由文件 | 关键端点 |
| --- | --- | --- |
| 自动化规则 / 调度 | `backend/app/gateway/routers/automation_scheduler.py`（`APIRouter` prefix `/api/automation`） | `POST /tasks/{id}/run`（手动触发）、规则 CRUD、scheduler 启停 |
| Task Center 任务 | `backend/app/gateway/routers/tasks.py`（prefix `/api/tasks`） | `GET /{task_id}:994`、`POST /{task_id}/start:1610`、`POST /{task_id}/stop:1655`、`POST /{task_id}/resume:1676`、`POST /{task_id}/restart:1753`、`POST /{task_id}/cancel:1971`、`POST /{task_id}/retry:2466`、`GET /{task_id}/execution-history:2604`、`GET /{task_id}/runtime:2674`、`GET /queue/status:923`、`POST /queue/tick:932` |
| 状态出口 / SSE | `backend/app/gateway/routers/events.py`（`/api/events`） | `/tasks/{task_id}/stream` 等 SSE；事件词汇定义在 `backend/app/gateway/events/task_events.py`（141 行） |

`rg` 命中分布（`rg-hits.txt` 统计，共 128 行）：`events/task_event_handlers.py` 21、`routers/tasks.py` 17、`events/task_events.py` 9、`task_runtime_degrade.py` 8、`events/__init__.py` 8、`task_runtime_optin.py` 6、`routers/automation_scheduler.py` 6、`automation_runner.py` 6，其余 15 个文件各 1～5 行。

### 1.1 无人值守（unattended）任务链

- 队列调度循环：`backend/app/gateway/task_queue_runner.py:155 run_task_queue_scheduler`；单次 tick `:71 task_queue_tick`。
- 队列开关：`EVOFLOW_TASK_QUEUE_ENABLED`（`task_queue_runner.py:25`）、`EVOFLOW_TASK_QUEUE_MAX_CONCURRENT`、`EVOFLOW_TASK_QUEUE_TICK_SECONDS`。
- 推进实现：`backend/app/gateway/unattended_task_pipeline.py:636 _advance_unattended_task_impl`，对外入口 `:875 advance_unattended_task`。
- 候选/在途筛选：`:882 list_unattended_candidates`、`:912 list_unattended_in_progress`、`:958 count_active_unattended_tasks`。
- 无人值守判定：`:68 is_unattended_task`（`run_mode == "unattended"`）。

---

## 2. 旧业务事实源与写路径

### 2.1 自动化规则与运行记录

| 事实源 | 位置 | 说明 |
| --- | --- | --- |
| 规则定义（源） | TOML `~/.evoflow/tasks/automations` | 文件为源 |
| 规则镜像 | `evoflow_automations` | PostgreSQL 镜像，`backend/packages/harness/evoflow/persistence/automation_repositories.py` |
| 运行记录 | `evoflow_automation_runs` | 写入集中在 `automation_repositories.append_automation_run`；调用方均在 `backend/app/gateway/automation_runner.py`（1516 行） |

### 2.2 Task Center 任务行（本域 A03 的唯一回写目标）

- 存储：既有 project storage，读取 `get_project_storage()` / `find_main_task`，写入 `_save_task_row:175`、`_patch_task:181`（`unattended_task_pipeline.py`）。
- 状态字段：任务行 `status`、`unattended_stage`、`progress`、`unattended_attempts`、`execution_authorized` / `authorized_at` / `authorized_by`、`error`、`completed_at` / `failed_at`。
- 历史字段：任务行 `execution_history`（list[dict]），读取出口 `tasks.py:2618`（`GET /api/tasks/{task_id}/execution-history`）。
- 扩展槽：任务行 extras 中承载 `runtime_run_linkage` 与 `runtime_run_cursor`（`routers/tasks.py:52-59` 明确登记这两个 key 由 Runtime 域独占）。
- 终态收敛：`unattended_task_pipeline.py:457 _finalize_executing_task`（子任务全终态 → 主任务 `completed` / `failed`）；重试再入队 `_requeue`（`:660-679`，清空 plan/subtasks/授权并把 `unattended_attempts + 1`）。

### 2.3 旧状态机

| 对象 | 位置 | 取值 |
| --- | --- | --- |
| `TaskStatus` | `backend/packages/harness/evoflow/collab/models.py:59` | `inbox / pending / planning / planned / executing / paused / reviewed / completed / failed / cancelled` |
| `CollabPhase` | 同上 `:23` | 协同阶段 |
| 迁移规则 | `backend/packages/harness/evoflow/collab/state_transitions.py` | `can_transition:102`、`validate_transition:116`、`get_allowed_transitions:130` |

### 2.4 既有事件词汇

`backend/app/gateway/events/task_events.py` 定义 `TaskAuthorizedEvent`、`TaskExecutionStartedEvent`、`TaskExecutionFailedEvent`、`TaskCancelEvent`、`TaskCancelledEvent`、`TaskResumeEvent`；消费者在 `backend/app/gateway/events/task_event_handlers.py`（752 行，本域 `rg` 命中最多）。

---

## 3. Runtime bridge 的实际边界

### 3.1 生产代码中已接线的 Runtime 调用点 —— 共 3 处

| 作用 | 位置 | 说明 |
| --- | --- | --- |
| 建立 / 复用 Run | `backend/app/gateway/unattended_task_pipeline.py:599 _maybe_run_via_runtime` | `:608` 引入 `task_runtime_optin`；`:610` 开关短路；`:614` 调 `establish_runtime_run`；`:624-625` 用 `_patch_task` 写回 linkage |
| 队列 tick 跳过已关联任务 | `backend/app/gateway/task_queue_runner.py:119 decide_runtime_pickup` | `:120-135` 命中 `should_skip` 即 `continue`，只记 `runtime_run_id` |
| 取消已关联任务 | `backend/app/gateway/routers/tasks.py:1919 _cancel_linked_runtime_run_if_any` | `:1928-1930` 引入 `task_runtime_optin` / `task_runtime_cancel` / `task_runtime_context`；`:1947` 调 `cancel_linked_runtime_run`；`:1953` 写回 |

### 3.2 服务器侧开关

- 变量：`EVOFLOW_AUTOMATION_UNATTENDED_RUNTIME`（`task_runtime_optin.py:100`），truthy 集合 `{"1","true","yes","on"}`，默认关闭。
- 判定：`task_runtime_optin.py:168 runtime_unattended_enabled`；路由决策 `:215 decide_runtime_opt_in`（开关 → 显式 opt-out → 非法值不 opt-in → 默认由开关决定）。
- 任务级执行模式：`:194 classify_execution_mode`，opt-in 值 `task_center / plan / unattended / task_center_plan`，opt-out 值 `direct / direct_langgraph / langgraph / execute / runs_wait`。

### 3.3 可复用的 Runtime 公开契约（只登记，不修改）

`backend/app/qagent_runtime/service.py`：`create_run:70`（接受 `org_id`）、`start_run:85`、`get_run_status:301`、`stream_events:304`、`request_cancel:324`、`resume_run:337`、`get_result:374`；事件词汇 `backend/app/qagent_runtime/events.py:13 EventType`、`:29 TERMINAL_STATUSES`。

`task_runtime_optin.default_runtime_contract():247` 已把 `RuntimeService(RuntimeRepository.from_config())` 封装为进程内单例，供 bridge 复用。

---

## 4. 已实现但零生产调用点（本域 A03 的唯一依据）

以下模块均已实现并有单测，但除测试与彼此引用外**没有任何生产调用点**：

| 模块 | 关键符号 | 职责 |
| --- | --- | --- |
| `task_runtime_projection.py` | `project_runtime_status:354`、`build_runtime_history_record:322`、`task_status_for_runtime_status:139` | Runtime 状态 → 既有 `TaskStatus` 与 `execution_history` 的投影 |
| `task_runtime_cursor.py` | `read_runtime_cursor:139`、`advance_runtime_cursor:164` | 每 Run 事件流水位（watermark） |
| `task_runtime_reconcile.py` | `reconcile_linked_task_runtime:102` | 已关联任务的幂等再投影（只读 Runtime，不创建 Run） |
| `task_runtime_degrade.py` | `assess_runtime_link:173`、`reconcile_task_runtime_safely:264` | 坏链路的 fail-safe 展示 |
| `task_runtime_result.py` | `sanitize_result_summary` | 结果摘要白名单脱敏 |
| `task_runtime_failure.py` | `sanitize_error_code`、`sanitize_failure_detail` | 失败详情脱敏 |
| `task_runtime_event_bridge.py` | `apply_runtime_event:128` | Runtime Event v1 帧 → 状态投影 |
| `task_runtime_recovery.py` | `recover_task_stream:200` | 断连后恢复 Task Center 视图 |
| `task_runtime_asset.py` | — | 资产元数据展示边界（不落二进制） |
| `task_runtime_input_snapshot.py` | — | 输入快照展示边界（非身份字段白名单） |

已接线部分（`task_runtime_optin.py`、`task_runtime_linkage.py`、`task_runtime_context.py`、`task_runtime_schedule.py`、`task_runtime_cancel.py`）不在此列。

### 4.1 直接后果（现状缺口）

1. Run 建立后，队列 tick 因 `decide_runtime_pickup` 返回 `already_scheduled`（`task_runtime_schedule.py:145-151`）而不再推进该任务；
2. **没有任何代码把 Run 状态投影回任务行**，因此 `completed` / `failed` / `cancelled` / `timed_out` 终态与结果**当前不会出现在既有 `GET /api/tasks/{task_id}` 与 `/execution-history` 中**；
3. 按执行计划 §2.5.3 的 G2.1 单卡完成标准（真实入口 → Runtime 执行 → 终态回写 → 原 API 可读），该路径尚未闭环。

该事实是 `AG-G2-AUTO-003-A01` 可实例化的唯一依据。

---

## 5. 未迁移的旧路径（A03 不覆盖，保持原样）

1. App Runner 绑定工作流自动化：`automation_runner._run_bound_app_for_automation`。
2. prompt-only 默认直连 LangGraph：`automation_runner._run_one_task_direct_langgraph`。
3. 调度器自身（`routers/automation_scheduler.py`）不产生 Runtime Run；`POST /api/automation/tasks/{id}/run` 走既有 automation runner 路径。

---

## 6. 缺口与受 G0 阻塞项（只登记，不实现）

| 缺口 / 事项 | 现状 | 阻塞决策 |
| --- | --- | --- |
| 无人值守 Run 创建时的组织归属 | `task_runtime_optin.resolve_server_task_runtime_identity:269` 从任务行持久化 ACL 列取 `org_id` / `scope_id` / `created_by`，三者任一为空即返回 `None` → 走 legacy（`trusted_identity_unavailable`）。**本卡只登记该路径，不修正语义**；若实际环境出现默认 `org_id="local"`，属于缺口而非本卡范围 | —（登记为缺口） |
| `RuntimeContract.create_run` 未声明 `org_id` | `backend/app/qagent_runtime/contract.py:32` 未声明，而 `service.create_run` 接受该关键字；`task_runtime_optin` 为此自带 `RuntimeRunContract` Protocol（`:127`） | —（登记为缺口，本批不改契约） |
| Task Center cancel 后 approval 终态 | 既有 `_cancel_linked_runtime_run_if_any` 只做取消转发与写回 | `G0-DEC-001` |
| 执行所有权 / 跨实例触发幂等 | 现有去重完全依赖持久化 linkage + Runtime 自身幂等键（`task_runtime_schedule.py:27-31` 显式声明不做 leader election / lease / fencing） | `G0-DEC-002` |
| Event v1 流式帧 / sequence / cursor / gap | 投影层有 `sequence` 字段与 `runtime_run_cursor`，但**无流式订阅生产接线** | `G0-DEC-003` |
| Recovery Point / attempt 与重启恢复 | `task_runtime_recovery.py`、`task_runtime_reconcile.py` 已实现但零生产调用点；重启对账未实例化 | `G0-DEC-004` |

---

## 7. 结论

- **可实例化**：`AG-G2-AUTO-003-A01`（终态回写与既有读取出口）、`AG-G2-AUTO-004-A01`（定向测试）、`AG-G2-AUTO-005-A01`（真实 HTTP 验收 Runbook）。判据：既有真实旧文件 + 已存在的 Runtime 调用点（§3.1 三处）+ 唯一业务事实源已定位（§2.2 任务行）。
- **不重派**：Run 建立与关联（`433a9df`，`AG-G2-AUTO-003-B01`）已完成。
- **不实例化**：Event 流式续读 / cursor / gap、跨实例触发幂等、cancel 与 approval 耦合、重启对账、灰度观察期（`G2-AUTO-006`）。
- **A03 允许修改范围**：`backend/app/gateway/unattended_task_pipeline.py`、`backend/app/gateway/task_queue_runner.py` + 新增 `backend/app/tests/test_task_runtime_terminal_writeback.py`（生产文件 2 个，未用满上限）。
