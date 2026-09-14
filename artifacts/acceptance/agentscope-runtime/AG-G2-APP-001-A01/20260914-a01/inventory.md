# AG-G2-APP-001-A01 Inventory

- 卡片：`AG-G2-APP-001-A01`（母任务 `G2-APP-001`，§2.5.4 App 入口三卡之首）
- 分支：`codex/agentscope-runtime`
- 起始 HEAD：`5842058c6f57c7a59285517ea17500ab71ff019d`（与 `origin/codex/agentscope-runtime` 一致）
- 性质：**只读盘点**。未修改任何代码、schema、migration、Gateway 主干、router registry、`backend/app/qagent_runtime/`、LangGraph 主链、前端；未删除/替换 App Runner / Workflow / LangGraph 旧入口。
- 行号对应本次盘点 HEAD，仅作定位用途；后续卡片若改动这些文件需重新核对。

## 范围与证据方法

- 只读检索范围：`backend/app/gateway/routers/`（apps.py、openai_compat_apps.py）、`backend/app/gateway/`（automation_runner、task_runtime_* 对照）、`backend/packages/harness/evoflow/collab/app_runner.py`、`backend/packages/harness/evoflow/persistence/app_repositories.py`、`backend/app/qagent_runtime/service.py`、`backend/app/gateway/router_registry.py`。
- 方法：受控 `rg` 关键词（`app_runner` / `run_app` / `save_run` / `update_run_status` / `RuntimeService` / `qagent_runtime`）+ 精确 `sed` 读取命中点及其最多两个直接调用点。未做全仓扫描、未建索引、未读前端。
- 对照材料（仅作参考证据，未重复实现）：`docs/plan/agentscope-2-g2-chat-entry-map.md`、`docs/plan/agentscope-2-g2-automation-entry-map.md`。
- 未执行的禁止操作：`git switch/checkout/pull/merge/reset/rebase/restore/clean/stash`；删分支/worktree；force push；修改任何仓库文件（本文档除外）。

## 真实用户入口

| # | 入口 | 方式 | 位置 | 最短调用链 |
|---|---|---|---|---|
| 1 | **App 详情页「运行」按钮** | `POST /api/apps/{app_id}/run` | `backend/app/gateway/routers/apps.py:486` `run_app_endpoint`（router 前缀 `/api/apps`，`apps.py:24`；`Depends(require_premium)`） | `run_app_endpoint` → `require_app_visible` → `asyncio.to_thread(run_app, ...)`（`apps.py:502`）→ `evoflow.collab.app_runner.run_app`（`app_runner.py:843`） |
| 2 | OpenAI 兼容 API（App 发布为模型） | `POST /v1/chat/completions` | `backend/app/gateway/routers/openai_compat_apps.py:281`（router 前缀 `/v1`，`:42`） | `_start_published_app` → `run_app(..., execution_mode="workflow", run_kind="production", trigger_kind="api")`（`openai_compat_apps.py:193`）→ 同一 `run_app` |
| 3 | 自动化绑定工作流（scheduled） | 内部调度触发，非用户直连 | `backend/app/gateway/automation_runner.py:766` `_run_bound_app_for_automation` → `run_app`（`:796`） | 已由 Automation / Task Center 首轮接入覆盖，**不属于本卡重复范围**，仅登记 |

状态读取入口（回写后的用户可见出口）：`GET /api/apps/runs/{run_id}`（`apps.py:585` `get_run_status_endpoint`）→ `get_run_status`（`app_runner.py:920`）；列表 `GET /api/apps/{app_id}/runs`（`apps.py:556`）。

**推荐作为 A03 首次闭环入口：入口 1 —— `POST /api/apps/{app_id}/run`（`run_app_endpoint`）。**

理由：①用户真实点击入口，`trigger_kind="manual"`，鉴权与 org 归属判定齐备（`require_app_visible`）；②与入口 2 汇聚到同一个 `run_app()` 收敛点，改一处即覆盖 workflow 模式主路径；③automation 路径（入口 3）已由已集成的 Automation 域覆盖，不应再触碰。入口 2 与入口 3 本卡不改，后续语义卡再议。

`run_app` 分派语义（`app_runner.py:843-918`）：`execution_mode="lead_supervised"` → `run_app_lead_supervised`（`:544`，绑定聊天 thread，依赖 LangGraph 主链，本卡不接入）；`execution_mode="workflow"` → `run_app_workflow`（`:639`，创建 Task Center 主任务 + 子任务 DAG → `apply_workflow_dispatch`（`:134`）同步派发）。**A03 首次闭环建议只覆盖 workflow 模式**，与 Automation 域已接入形态对齐。

## 旧业务 Run 记录与状态写路径

记录类型：**`evoflow_app_runs`**（App Run 执行记录，SQLite，经 `evoflow.persistence.app_repositories` 读写），与 Task Center 主任务通过 `task_id` 关联（`load_run_by_task_id`，`app_repositories.py:527`）。

字段（`save_run` INSERT，`app_repositories.py:484-508`）：

| 字段 | 语义 |
|---|---|
| `id` | 主键，`_make_run_id()`（`app_runner.py:363`）生成 |
| `app_id` / `app_version` | 关联 App 定义与固定版本 |
| `task_id` | 关联 Task Center 主任务 |
| `thread_id` | lead_supervised 模式绑定聊天 thread（workflow 模式为 NULL） |
| `parameters_json` / `execution_mode` | 输入参数与执行模式 |
| `status` | 状态：`planned → executing → completed / failed / cancelled`（另有 `paused`、`plan_ready`） |
| `progress` | 0–100 |
| `result_summary` / `error` | 结果摘要 / 错误（终态内容） |
| `created_at` / `started_at` / `completed_at` | 时间戳；`completed_at` 在终态自动落（`update_run_status`，`app_repositories.py:540-559`） |

终态集合与回写语义（`update_run_status`，`app_repositories.py:540`）：`completed` / `failed` / `cancelled` 为终态，写入时自动补 `completed_at`。

写点清单（全部可回溯）：

| 写点 | 位置 | 说明 |
|---|---|---|
| 创建写点（workflow） | `app_runner.py:810` `app_repositories.save_run(... status="planned")` | `run_app_workflow` 第 10 步，run 记录与 Task Center 行同时就位 |
| 创建写点（lead_supervised） | `app_runner.py:592` `save_run(...)` | 绑定 thread 分支 |
| 进行中写点 | `app_runner.py:511-538` `_sync_run_from_task` → `update_run_status`（`:525`） | Task Center 状态经 `_TASK_STATUS_TO_RUN`（`:496`）映射回写：`executing`/`running→executing`、`planned`、`plan_ready`、`paused` |
| 派发异常写点 | `app_runner.py:272` `update_run_status(run_id, "executing", error=...)` | `_schedule_workflow_dispatch` 同步派发失败时记 error 并转队列重试；Task Center 行由 `_persist_workflow_dispatch_outcome`（`:162`）记 `dispatch_retry` |
| 成功写点 | `app_runner.py:1073`（`get_run_status` 轮询内 `_sync_run_from_task`）→ `:525`/`:531` `update_run_status(..., "completed", progress=100)` | **轮询驱动**：用户/前端读 `GET /api/apps/runs/{run_id}` 时把 Task Center 终态映射落库 |
| 结果摘要写点 | `app_runner.py:1116` `update_run_status(..., result_summary=...)` | `get_run_status` 内终态时合成 `result_summary` 回写 |
| 取消写点 | `app_runner.py:1203` `update_run_status(run_id, "cancelled", error=reason)` | `cancel_run`（`:1146`） |
| 暂停/恢复写点 | `app_runner.py:1237`（`pause_run` → `paused`）、`:1268`（`resume_run` → `running`） | — |

**未发现项（如实登记）**：未发现独立的「失败显式写点」——`failed` 终态同样依赖轮询路径 `_sync_run_from_task` 映射 Task Center 的 `failed` 状态落库；`error` 字段仅在 dispatch 异常（`:272`）与取消（`:1203`）显式写入。未发现按 `runtime run_id` 关联的字段或第二状态源。

## 可复用 Runtime 公开调用点

**已证实可复用**（本分支 HEAD 核对）：

- 服务类 `RuntimeService`：`backend/app/qagent_runtime/service.py:33`。公开方法：
  - `create_run(*, org_id, task_id, input_payload, idempotency_key=None)`（`service.py:70`）
  - `start_run(run_id, org_id=None)`（`:85`）
  - `stream_events(run_id, *, after_sequence=0, org_id=None)`（`:304`）
  - `request_cancel(run_id, org_id=None)`（`:324`）
  - `resume_run(run_id, org_id=None)`（`:337`）
  - `get_result(run_id, org_id=None)`（`:374`）
- HTTP 面**已在 Gateway 挂载**：`backend/app/gateway/router_registry.py:88` `_include_module_router(app, "app.qagent_runtime.router", label="qagent_runtime")`，前缀 `/api/qagent/runtime`。**无需改 Gateway 主干即可调用。**
- 鉴权：`create_run` 的 `org_id` 由服务端 principal 解析（参照 chat entry map §5.1 已验证结论），客户端不可伪造组织归属。
- `create_run` 支持组织级幂等键（`idempotency_key`，G0-IDEM-001 已 Done），可用于把旧业务 `run_id` 桥接为幂等关联，**无需新增 schema 字段**。

**已集成参考模式（仅作对照证据，本卡不得重复实现）**：Automation 域的 `task_runtime_*.py` 系列（如 `task_runtime_projection.py`，`:292` 标记 `"source": "qagent_runtime"`）与 Chat 域的 `chat_runtime_*.py` 系列均在 **gateway 层**以 opt-in / bridge / projection 文件接入 Runtime，未修改 `backend/app/qagent_runtime/` 内核。App 域可复用同一形态。

**仅推测、不得用于 A03**：`stream_events` 的 SSE 事件词汇与 App 步骤级粒度的映射、`resume_run` 对 workflow DAG 断点恢复的适用性——本卡未验证，属 AG-G2-APP-002（映射卡）范围。

## 最小首次闭环建议

A03 边界（仅为边界描述，不实现）：

1. 一个真实入口：`POST /api/apps/{app_id}/run`（workflow 模式）在 `run_app_workflow` 创建旧 `evoflow_app_runs` 记录后，经 **opt-in 开关**调用一次 Runtime（`create_run` + `start_run`，幂等键绑定旧 `run_id`）。
2. Runtime 可观察终态（`get_result` / 终态事件）映射回原 `evoflow_app_runs.status`（`completed` / `failed` / `cancelled`）与 `result_summary` / `error`，复用既有 `update_run_status` 写点，`completed_at` 语义不变。
3. 保持现有用户入口、`GET /api/apps/runs/{run_id}` 读取语义、Task Center 关联、LangGraph 旧链路与 API 完全可用；开关关闭时行为与现状逐字节一致。
4. 不新增第二状态源：Runtime 侧为执行事实源，`evoflow_app_runs` 状态为兼容投影；映射关系通过幂等键重建，不新增 schema 字段。
5. 不得静默 fallback：Runtime 失败必须让原入口得到明确、可读的失败结果（§2.5.2）。

## AG-G2-APP-003-A01 候选允许修改文件

| # | 文件路径 | 当前职责 | A03 为什么必须修改 | 允许修改边界 | 明确不改的相邻范围 |
|---|---|---|---|---|---|
| 1 | `backend/packages/harness/evoflow/collab/app_runner.py` | App Run 统一入口与状态回写（`run_app` / `run_app_workflow` / `_sync_run_from_task`） | Runtime 调用必须发生在旧 Run 记录创建后的收敛点（`:810` 之后）；终态回写复用 `_sync_run_from_task` 所在轮询链 | 仅在 `run_app_workflow`（或 `run_app` workflow 分支）插入 opt-in 的 Runtime 触发调用；在轮询回写处接入终态投影。**不改** `run_app_lead_supervised`、`_create_lead_thread`、`_TASK_STATUS_TO_RUN` 映射语义 | LangGraph 依赖、Task Center 状态机、automation 调用路径 |
| 2 | `backend/app/gateway/app_runtime_bridge.py`（**新建**） | App 域 Runtime 桥接：opt-in 开关、`RuntimeService.create_run/start_run/get_result` 封装、终态映射、幂等键构造（`app-run:{run_id}`） | 参照已集成的 `task_runtime_*.py` 形态，桥接逻辑必须独立成文件，避免散落在 runner 内 | 新建文件；只调用 `RuntimeService` 公开方法与 `app_repositories` 公开函数；含开关默认关闭逻辑 | 不 import AgentScope 私有对象；不改 Runtime 内核 |
| 3 | `backend/packages/harness/evoflow/persistence/app_repositories.py` | `evoflow_app_runs` CRUD（`save_run` / `update_run_status`） | 终态回写复用现有 `update_run_status`；若桥接需要按现有字段查询（如按 `task_id`/`id`）补一个只读查询函数 | 仅允许新增只读查询或对 `update_run_status` 的调用适配；**不得新增字段、不得改表结构、不得写 migration** | `evoflow_apps` 等其他表、其他 repository |
| 4 | `backend/app/gateway/routers/apps.py` | `run_app_endpoint`（`:486`）与状态读取端点 | 仅当 Runtime 触发需要 async 上下文时，允许在端点层做最小 async 适配或透传开关上下文 | 最小 diff；端点路径、鉴权、请求/响应模型不变 | 其他端点（keys、revisions、apps CRUD） |
| 5 | `backend/app/tests/test_app_runtime_bridge.py`（**新建**） | 聚焦测试：opt-in 开启时入口 → Runtime 执行 → 终态回写；关闭时行为不变 | A03 完成标准要求至少一个聚焦该入口的自动化测试 | 新建测试文件；可用离线 fake provider 既有 fixture | 不改既有测试；不跑全仓测试 |

共 5 个（2 新建 + 3 既有），≤6。不需要修改 `router_registry.py`（Runtime router 已挂载）、`background_startup.py`、`qagent_runtime/`、migrations、前端。

## 未决项与停止条件

以下为真实发现的语义问题，需负责人在 A02（映射卡）拍板或冻结，本卡不设计解决方案：

1. **失败终态依赖轮询**：现有 `failed` 终态没有显式写点，靠 `get_run_status` 轮询 Task Center 状态映射。A03 若要求「Runtime 执行完成即回写终态」，需决定是沿用轮询链还是由 bridge 主动回写；两者不能同时成为事实源。
2. **Runtime 失败时的入口语义**：`run_app` 返回 `{run_id, task_id, status, ...}` 是既有契约。Runtime 调用失败（非任务失败）时入口应返回什么状态，需负责人明确「明确、可读的失败结果」的具体形态（§2.5.2），不得静默 fallback 到 LangGraph。
3. **`task_id` 关联**：`create_run` 的 `task_id` 参数与 Task Center 主任务 `task_id` 的关系（复用同一 ID 或以旧 `run_id` 为幂等键）属映射决策，归 AG-G2-APP-002。
4. 未发现需要改 Runtime 内核、新增 schema/migration 或第二状态源的情况；未触发任何 Blocked 停止条件（入口唯一、候选文件 ≤6）。
