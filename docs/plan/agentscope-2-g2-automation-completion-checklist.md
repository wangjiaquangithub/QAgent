# G2 Automation / Task Center 域完成清单

- 卡片：`AG-G2-AUTO-007-B02`（**文档卡，仅此一份文档，不含任何实现改动**）
- 分支：`codex/g2-automation-task-center`
- 基线：`origin/codex/agentscope-runtime-migration`（`b09adf4`）
- 用途：把本域「已接入的 opt-in 路径」与「尚未迁移的自动化路径」一次性列清，避免后续验收或汇报时把未迁移能力当作已实现。

---

## 1. 已接入：unattended task opt-in 路径

### 1.1 切口定义

一条**真实的、已存在的**最小闭环，默认关闭：

```
既有 Task Center / unattended 主任务
  → advance_unattended_task（既有执行收敛点）
     → 默认关闭的服务端开关判定
     → 经 QAgent Runtime 公开契约创建 / 复用 Run（PostgreSQL 为唯一事实源）
     → 在任务行既有的扩展槽位写入兼容关联 runtime_run_linkage
  → Runtime 事件 / 状态 → 既有 execution_history + 既有 TaskStatus 收敛
  → 既有取消入口 → Runtime 公开取消调用
```

- **开关**：`EVOFLOW_AUTOMATION_UNATTENDED_RUNTIME`，服务端读取，默认关闭；
- **不改前端、不改全局 SSE、不改 `/api/langgraph`、不改 `qagent_runtime`、不新增表或 migration**；
- **SQLite 只保存一个兼容关联指针**（`extra_json` 内的 `runtime_run_linkage`）。Runtime 的 Run、Event、Result、Asset、Recovery Point 一律以 **PostgreSQL 为唯一事实源**。

### 1.2 交付物（按模块）

| 模块 | 作用 |
|---|---|
| `backend/app/gateway/task_runtime_context.py` | 可信任务上下文 → Runtime 请求的适配边界：只取服务端已解析的身份/组织/可见性，产出确定性的 `runtime_task_id` 与 `idempotency_key`（按组织分区），前置不足即拒绝 |
| `backend/app/gateway/task_runtime_linkage.py` | 在任务行**既有**扩展槽位建立/读取 `runtime_run_id` 关联；重复触发复用或安全拒绝；跨组织、跨任务读取一律拒绝 |
| `backend/app/gateway/task_runtime_optin.py` | 默认关闭的 opt-in 决策与 Run 建立；Runtime 失败向上抛出，绝不降级为第二次旧链路执行 |
| `backend/app/gateway/task_runtime_projection.py` | Runtime 状态 → 既有 TaskStatus + 既有 `execution_history` 的局部投影；终态不倒退、旧事件不覆盖新终态 |
| `backend/app/gateway/task_runtime_event_bridge.py` | Runtime Event v1 帧 → 既有历史记录的桥接；重放不重复、乱序不倒退 |
| `backend/app/gateway/task_runtime_cancel.py` | 已关联任务的取消走 Runtime 公开取消语义；未关联任务保持旧路径；重复取消幂等 |
| `backend/app/gateway/unattended_task_pipeline.py` | 收敛点 4 行接线 + 1 个自包含 helper（原路径在开关关闭时逐字节不变） |
| `backend/app/gateway/routers/tasks.py` | 既有 `POST /api/tasks/{id}/cancel` 增加一次 helper 调用；未关联任务不产生任何 Runtime 调用 |

### 1.3 实现文件（可核对）

```
backend/app/gateway/task_runtime_context.py
backend/app/gateway/task_runtime_linkage.py
backend/app/gateway/task_runtime_optin.py
backend/app/gateway/task_runtime_projection.py
backend/app/gateway/task_runtime_event_bridge.py
backend/app/gateway/task_runtime_cancel.py
backend/app/gateway/unattended_task_pipeline.py      (局部接线)
backend/app/gateway/routers/tasks.py                 (局部接线)
backend/app/tests/test_task_runtime_context.py
backend/app/tests/test_task_runtime_linkage.py
backend/app/tests/test_task_runtime_optin.py
backend/app/tests/test_task_runtime_projection.py
backend/app/tests/test_task_runtime_event_bridge.py
backend/app/tests/test_task_runtime_cancel.py
backend/app/tests/test_task_runtime_e2e_boundary.py
docs/plan/agentscope-2-g2-automation-manual-acceptance.md
docs/plan/agentscope-2-g2-automation-entry-map.md
```

### 1.4 已冻结的状态语义（已实现，勿再争议）

| Runtime 状态 | Task 状态 | 历史 |
|---|---|---|
| `waiting_approval` | **不变**（既不 `paused` 也不 `planned`） | 追加 `runtime_status=waiting_approval`、`approval_required=true` |
| `timed_out` | `failed` | 保留 `runtime_status=timed_out` + 脱敏 `error_code` / `reason` |
| `completed` / `failed` / `cancelled` | 同名收敛 | 追加对应记录 |
| 其他非终态 | 映射到既有值（`pending` / `planning` / `executing`） | 追加对应记录 |

未新增 `TaskStatus` 枚举值（现有集合为 `inbox / pending / planning / planned / executing / paused / reviewed / completed / failed / cancelled`）。

### 1.5 交付提交

| SHA | 提交 | 对应卡片 |
|---|---|---|
| `70f48f9` | `docs(automation): map task center integration entrypoints` | `G2-AUTO-001` |
| `c8c56b3` | `feat(automation): add trusted task runtime context adapter` | `AG-G2-AUTO-002-B01` |
| `3f02167` | `feat(automation): persist task runtime run linkage` | `AG-G2-AUTO-002-B02` |
| `7b30ac1` | `feat(automation): opt in unattended tasks to runtime` | `AG-G2-AUTO-003-B01` |
| `3e5eac1` | `feat(automation): project runtime status into task center` | `AG-G2-AUTO-003-B02` |
| `95bb0e7` | `feat(automation): bridge runtime events to task stream` | `AG-G2-AUTO-004-B02` |
| `dd17dce` | `feat(automation): route linked task cancel to runtime` | `AG-G2-AUTO-005-B02` |
| `c73d32a` | `docs(automation): add runtime task center acceptance steps` | `AG-G2-AUTO-006-B02` |

### 1.6 验收入口

完整可执行步骤见 `docs/plan/agentscope-2-g2-automation-manual-acceptance.md`。要点：

- 开关：`EVOFLOW_AUTOMATION_UNATTENDED_RUNTIME=1`（`1/true/yes/on`；默认关闭）；
- 触发：既有 Task Center 创建 `run_mode=unattended` 任务 + 既有队列推进；
- 观察：既有 `GET /api/tasks/{id}`（状态）与既有 `GET /api/tasks/{id}/execution-history`（历史）；
- 核对：PostgreSQL `qagent_runs` / `qagent_run_events` 与 SQLite `evoflow_collab_tasks.extra_json`；
- 回退：`unset EVOFLOW_AUTOMATION_UNATTENDED_RUNTIME`，**不删除任何历史数据**。

---

## 2. 尚未迁移：三条真实自动化路径

以下路径**保持原样，未接入 Runtime**。任何汇报或验收都不得声称它们已迁移。

| # | 路径 | 真实入口（证据） | 当前行为 | 未迁移原因 |
|---|---|---|---|---|
| 1 | **App Runner 绑定工作流** | `automation_runner._run_one_task` → `_run_bound_app_for_automation` → `evoflow.collab.app_runner.run_app` | 仍走原有 collab 任务（`run_mode=unattended`）与 `task_tool` DAG | 绑定工作流有独立的渲染/DAG 语义，接入需先冻结 App Runner 的 Run 映射，本域未冻结 |
| 2 | **prompt-only 规则默认直连 LangGraph** | `automation_runner._run_one_task_direct_langgraph` → `_invoke_langgraph_for_automation` | 仍直连 LangGraph `runs.wait`（默认路径） | 直连路径绕过 Task Center 状态机，接入等价于改动 LangGraph 主链调用语义，超出本域边界 |
| 3 | **scheduler 定时触发** | `automation_runner.automation_tick` / `run_automation_scheduler`（开关 `automation_scheduler_enabled`） | 仍按 cron 触发上述两条路径，自身不产生 Run | 定时器接入需先冻结「触发窗口 → Run」的跨实例幂等语义（leader/lease/fencing），属未冻结生产语义 |

补充说明（同样**未迁移**）：

- Task Center 无人值守队列的**另一条**调度循环 `task_queue_runner.run_task_queue_scheduler` 未做任何 Runtime 改造；本域只在它驱动的 `advance_unattended_task` 收敛点加了 opt-in 分支；
- 未实现多实例一致性（leader / lease / fencing）、HA、压测、Chaos；
- 未实现设备协议与离线回执；
- 未做任何前端页面改动。

---

## 3. 母任务对照

| 母任务 | 状态 | 说明 |
|---|---|---|
| `G2-AUTO-001` 盘点 | 完成 | 入口地图：`docs/plan/agentscope-2-g2-automation-entry-map.md` |
| `G2-AUTO-002` 映射 | 完成（本域口径） | 触发 → Run → attempt → claim → Event → 幂等的最小映射，落实为 context / linkage 两个模块与对应测试；**跨实例 claim 语义仍未冻结** |
| `G2-AUTO-003` 接入 | 完成（一条路径） | 仅在 unattended 收敛点接入 Runtime，默认关闭；其余两条路径未接入 |
| `G2-AUTO-004` 正式写入与重启恢复 | **部分** | Run/Event 以 PostgreSQL 为唯一事实源已达成；**重启恢复**（重启后把 Runtime 状态重新投影回 Task Center）未实现 |
| `G2-AUTO-005` 测试、回滚与对账 | **部分** | 本切口有 110 项直接测试与人工验收说明；**数据对账**（定时比对 Run 数与任务关联）未实现 |
| `G2-AUTO-006` 灰度与观察期 | **未开始** | 无租户级灰度开关、无观察期 metrics、无签署 verdict |

---

## 4. 未决事项与风险

| 级别 | 内容 | 位置 | 最小修复 |
|---|---|---|---|
| **P1** | 无人值守路径调用 `create_run` 时**未传组织**（Runtime 公开签名含 `org_id`，默认 `"local"`），导致 Run 行的组织归属落到默认值。幂等空间因本域已按组织分区而不会串号，但 Run 行归属不正确、按 `org_id` 过滤的 Runtime 查询会不一致。 | `task_runtime_optin.py` 调用处；`qagent_runtime/contract.py` 协议未声明 `org_id` | 协议声明 `org_id` + 调用处传入可信组织值 |
| P2 | 重启后无「Runtime 状态重新投影回 Task Center」的对账机制 | 本域未实现（Runtime 侧有 `recover_incomplete_runs`） | 单独开卡补对账；禁止在 SQLite 侧新建状态源 |
| P3 | `execution_history` 为自由结构列表，追加时不校验既有消费者字段假设（现有读取方均 `.get` 容错） | `task_runtime_projection.py` | 无阻塞 |
| P4 | `routers/tasks.py` 存在 3 个**改动前既有**的 ruff 告警（行 3 导入排序、行 1582 导入排序、行 2833 `F821 Undefined name HTTPException`），不在本域改动区域内，未修 | `backend/app/gateway/routers/tasks.py` | 与本域无关，建议由文件归属方统一处理 |

---

## 5. 范围声明

本域全程未改动：`backend/app/qagent_runtime/`、`backend/migrations/`、`backend/app/gateway/router_registry.py`、`backend/app/gateway/background_startup.py`、`/api/langgraph` 与 LangGraph 主链、Chat / Live Run 域、全局 SSE 基础设施、设备协议与 lease/fencing、前端、依赖与 lockfile。
