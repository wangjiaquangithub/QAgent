# AG-G2-APP-002-A01 App Run ↔ Runtime Run Mapping

- 卡片：`AG-G2-APP-002-A01`（依赖 `AG-G2-APP-001-A01`）
- 分支：`codex/agentscope-runtime`　基线：`b8cf3e0`（A01 提交）
- 性质：**只读字段级映射**。未修改任何代码、测试、配置、Plan、schema、migration 或 Runtime。
- 行号对应 HEAD `b8cf3e0`。

## 输入与范围

- 输入：`docs/plan/agentscope-2-enterprise-runtime-execution-plan.md`（AG-G2-APP-002-A01 卡定义，`:303`）、`artifacts/acceptance/agentscope-runtime/AG-G2-APP-001-A01/20260914-a01/inventory.md`。
- 受控检索范围（仅 A01 已登记区域 + 一个直接参考实现）：
  - `backend/packages/harness/evoflow/collab/app_runner.py`
  - `backend/packages/harness/evoflow/persistence/app_repositories.py`
  - `backend/app/gateway/routers/apps.py`
  - `backend/app/qagent_runtime/service.py`、`repository.py`、`events.py`（仅公开面：状态词汇、幂等、结果载荷）
  - 参考模式（不重复实现）：`backend/app/gateway/task_runtime_projection.py`（Automation 域已冻结的投影语义）
  - 授权上下文：`backend/packages/harness/evoflow/authz/context.py:103` `resolve_request_principal`
- 本卡不读前端；未做全仓扫描。

## App Run 生命周期与现有写点

全部引证自 A01 inventory（本卡在 HEAD `b8cf3e0` 复核过关键行号，一致）：

| 阶段 | 旧业务写点 | 位置 |
|---|---|---|
| 创建（workflow 模式） | `app_repositories.save_run(run_id, {..., "status": "planned", "progress": 0})` | `app_runner.py:810`（`run_app_workflow` 第 10 步） |
| 授权+派发 | `authorize_main_task_execution`（auto_authorize）→ `apply_workflow_dispatch` | `app_runner.py:787`、`:134` |
| 进行中 | `_sync_run_from_task` → `update_run_status(run_id, mapped, **fields)`；派发异常写 `executing`+error | `app_runner.py:525`、`:272` |
| 成功 | 轮询 `get_run_status` → `_sync_run_from_task` 映射 `completed`（progress=100）+ `result_summary` 回写 | `app_runner.py:1073`、`:525`/`:531`、`:1116` |
| 失败 | 同轮询链映射 `failed`（`_TASK_STATUS_TO_RUN:496` 含 `"failed"`）；`error` 字段仅 dispatch 异常（`:272`）显式写 | `app_runner.py:525` |
| 取消 | `cancel_run` → `update_run_status(run_id, "cancelled", error=reason)` | `app_runner.py:1146`→`:1203` |
| 状态字段约束 | `update_run_status` 接受任意 status 字符串，无枚举校验；终态 `completed`/`failed`/`cancelled` 自动落 `completed_at` | `app_repositories.py:540-559` |

## Runtime Run 公开生命周期与调用点

全部为公开契约（`backend/app/qagent_runtime/` 只调用、不修改）：

| 阶段 | 公开调用点 | Runtime 侧状态/事件 | 位置 |
|---|---|---|---|
| 创建 | `RuntimeService.create_run(*, org_id="local", task_id, input_payload, idempotency_key=None)` | 状态 `created` → `queued`；事件 `run.created`、`run.queued` | `service.py:70`；`repository.py:51-96`（`:83` created、`:96` queued）；`events.py:12-13` |
| 幂等 | 同 `org_id + idempotency_key` 命中即返回既有 run，**不创建第二个 Runtime Run** | — | `repository.py:61-70` |
| 启动 | `RuntimeService.start_run(run_id, org_id=None)` | `queued/created → planning`；有既存审批 → `waiting_approval`；否则规划后进入 `running/executing`；重复 start 不重复规划 | `service.py:85-134`；`events.py:14-21` |
| 事件流 | `RuntimeService.stream_events(run_id, *, after_sequence=0, org_id=None)` | Event v1：`run.running` / `run.executing` / `run.completed` / `run.failed` / `run.cancelled` | `service.py:304`；`events.py:17-23` |
| 状态读取 | `RuntimeService.status(run_id)` → `{run_id, task_id, status, version, created_at, updated_at, resume_count, plan, approval, error}` | — | `service.py:395-411` |
| 结果读取 | `RuntimeService.get_result(run_id)` → `{run_id, status, result, assets, error}`（`result`=result_payload，`error`=error_payload） | — | `service.py:374-383` |
| 取消 | `RuntimeService.request_cancel(run_id, org_id=None)` | 终态 `cancelled` | `service.py:324`；`events.py:23` |
| 终态词汇 | `TERMINAL_STATUSES = {completed, failed, cancelled, timed_out}`；`INCOMPLETE_STATUSES = {created, queued, planning, waiting_approval, running, executing}` | — | `events.py:26-27` |

org 解析（入口侧已证实）：`resolve_request_principal(request)` 返回 `Principal`，含 `org_id`（`authz/context.py:103`，JWT → webui identity，绝不静默回退 local admin）。

## 字段级映射表

| App Run 阶段 | 旧业务对象与字段 | 旧业务写点 | Runtime 调用点或事件 | Runtime 输入/输出字段 | 映射规则 | 幂等关联键 | 证据位置 | 证据等级 | A03 能否直接使用 |
|---|---|---|---|---|---|---|---|---|---|
| 创建 | `evoflow_app_runs.id`（`_make_run_id`） | `app_runner.py:810` `save_run` | `create_run` | 返回 `run_id`（Runtime 侧独立 id） | App Run id 不写入 Runtime 任何字段，经幂等键桥接 | **`app-run:{app_run_id}`**（由既有稳定 App Run id 派生；org 内唯一） | `app_runner.py:363`、`repository.py:61-70` | 已证实 | 是 |
| 创建 | `app_id`、`app_version`、`parameters` | `save_run` document（`app_repositories.py:484-508`） | `create_run(input_payload=...)` | `input_payload` 自由 JSON dict，无 schema 约束 | 整体放入 `input_payload`（`{"app_id", "app_version", "parameters", "execution_mode"}`），不新增任何存储 | 同上 | `repository.py:81` | 已证实 | 是 |
| 创建 | `task_id`（Task Center 主任务 id，`document["task_id"]`） | `app_runner.py:815` | `create_run(task_id=...)` | `task_id` 自由字符串关联字段 | 传 App Run 已关联的 Task Center 主任务 id（既有稳定标识，不造新 id） | 同上 | `app_repositories.py:499`、`repository.py:78` | 字段存在已证实；取值规则 A03 局部处理 | 是 |
| 创建 | `org_id`（旧记录无此字段） | 入口 `require_app_visible`（`apps.py:496`） | `create_run(org_id=...)` | org 隔离键 | 由服务端 `resolve_request_principal(request).org_id` 解析后传入；**禁止依赖默认值 `"local"`** | 幂等键在 org 内生效 | `authz/context.py:103`；`service.py:71`；`repository.py:62-66` | 已证实 | 是（org_id 传递需局部适配：`run_app` 增可选参数或 bridge 从端点层接收） |
| 创建 | `thread_id`（workflow 模式为 `NULL`） | `app_runner.py:816` | — | Runtime 无对应公开字段 | 不映射、不投影 | — | `app_repositories.py:490` | 已证实（无对应） | 是（保持 NULL） |
| 启动 | App Run 无独立启动写点（授权+派发同步完成） | `app_runner.py:787`、`:134` | `start_run(run_id)` | `planning → running/executing`；重复 start 安全 | create 后立即 start；start 幂等由 Runtime 状态机保证 | 同上 | `service.py:85-134`（`:96`、`:128-132`） | 已证实 | 是 |
| 进行中 | `status="executing"`、`progress` | `app_runner.py:272`、`:525` | 事件 `run.running` / `run.executing`（或 `status()` 轮询） | 无 progress 字段 | **progress 不从 Runtime 映射**，保持既有 Task Center 轮询路径；bridge 不额外写进行中状态 | 同上 | `events.py:20-21`；`service.py:395` | 状态词汇已证实；progress 无对应已证实 | 是（不投影 progress） |
| 成功 | `status="completed"`、`progress=100`、`result_summary`、`completed_at` | `app_runner.py:525`/`:531`、`:1116`（`update_run_status`） | `get_result(run_id)` → `status="completed"`、`result=result_payload` | `result` 自由 JSON | Runtime `completed` → `update_run_status(run_id, "completed", progress=100, result_summary=<从 result 提取>)`；`completed_at` 由 `update_run_status` 自动落（`app_repositories.py:553-555`） | 同上 | `service.py:374-383`；`app_repositories.py:540-559` | 已证实；result_summary 提取规则 A03 局部处理（参照 `task_runtime_result.sanitize_result_summary` 模式） | 是 |
| 失败 | `status="failed"`、`error`、`completed_at` | `app_runner.py:525`（映射路径）；`update_run_status` 无枚举限制 | `get_result(run_id)` → `status="failed"`、`error=error_payload{code,message}` | `error_payload` 结构化 | Runtime `failed` → `update_run_status(run_id, "failed", error=<sanitized message>)`；**不新增状态枚举**（`update_run_status` 本就接受 `"failed"`，`_TASK_STATUS_TO_RUN` 已含） | 同上 | `service.py:374-383`；`app_repositories.py:540`；`app_runner.py:496` | 已证实 | 是 |
| 取消 | `status="cancelled"`、`error=reason` | `app_runner.py:1203` | `request_cancel(run_id)` → `run.cancelled` | 终态 `cancelled` | Runtime `cancelled` → `update_run_status(run_id, "cancelled", error=<reason>)`，复用既有取消写点语义 | 同上 | `service.py:324`；`events.py:23`；`app_runner.py:1203` | 已证实 | 是 |
| 非终态回退 | 旧记录支持 `paused`/`running`（`resume_run` :1268） | `app_runner.py:1237`/`:1268` | `resume_run(run_id)` | Runtime 恢复语义 | **首次闭环不映射**（本卡范围外）；如 Runtime 处于 `waiting_approval`，App Run 状态不投影 | — | `service.py:337`；`task_runtime_projection.py:24-27`（automation 冻结先例） | 未证实（App 侧无对应场景） | 否（首次闭环不做） |

## 终态投影表

| App Run 状态 | App Run 写点 | Runtime 对应状态/事件 | 投影规则 | 证据等级 | A03 使用 |
|---|---|---|---|---|---|
| `planned` | `save_run` 初始（`app_runner.py:810`） | `created` → `queued`（`repository.py:83`/`:96`） | create_run 成功后 App Run 保持 `planned` 不变；Runtime 侧推进不回写非终态 | 已证实 | 是 |
| `executing` | `update_run_status`（`app_runner.py:525`/`:272`） | `running` / `executing`（`events.py:20-21`） | 不由 bridge 主动投影；沿用既有 Task Center 轮询写点，避免双写竞争 | 已证实（词汇）；投影策略为 A03 局部决策 | 是（不投影） |
| `completed` | `:525`/`:531`、result_summary `:1116` | `completed`（TERMINAL，`events.py:26`） | bridge 主动回写：`update_run_status(run_id,"completed",progress=100,result_summary=...)`，`completed_at` 自动落 | 已证实 | 是 |
| `failed` | 轮询映射 `:525`；`error` 写点 `:272` | `failed`（TERMINAL） | bridge 主动回写：`update_run_status(run_id,"failed",error=<sanitized>)`；**不创造新枚举** | 已证实 | 是 |
| `cancelled` | `cancel_run`（`:1203`） | `cancelled`（TERMINAL） | 复用既有 `cancel_run` 语义：bridge 侧取消调 Runtime `request_cancel`，回写走既有 `:1203` 写点 | 已证实 | 是 |
| （无对应） | — | `timed_out`（TERMINAL，`events.py:26`） | App Run 无该枚举；沿用 Automation 域已冻结语义 `timed_out → failed`（`task_runtime_projection.py:17-18`、`:76`） | Runtime 侧已证实；App 侧为已冻结对照语义，A03 局部处理 | 是（timed_out→failed） |
| （无对应） | — | `waiting_approval`（INCOMPLETE，`events.py:27`） | workflow 模式 `auto_authorize=True`（`app_runner.py:787`）；App Run 无审批中间态。首次闭环：出现时**不投影、不卡死**，视作非终态留给既有轮询 | 未证实（adapter 在该路径是否进入 waiting_approval 未验证） | 边界处理（不投影） |

**无法严格确认的字段/终态（如实登记）**：
1. `progress` 的中间值——Runtime 无 progress 概念，无法映射（保持既有轮询路径，不算缺口）。
2. `result_summary` 的精确提取规则——`result_payload` 结构未在本卡验证，A03 需局部定义提取（有 automation 先例模式可参照）。
3. `waiting_approval` 在 App workflow 路径是否会出现——未证实，A03 按非终态边界处理。
4. `thread_id`——workflow 模式恒为 NULL，无映射需求（已证实无对应）。

## A03 实现前置条件

**已满足**：
- 唯一推荐入口仍是 `POST /api/apps/{app_id}/run`（A01 结论，本卡未发现推翻证据）；
- `RuntimeService` 公开面（create/start/status/get_result/request_cancel）足够覆盖创建→启动→终态读取→取消全链；
- 幂等机制已证实：同 `org_id + idempotency_key` 不创建第二个 Runtime Run（`repository.py:61-70`）；
- App Run 全部终态写点齐备（`update_run_status` 接受 `completed`/`failed`/`cancelled` 并自动落 `completed_at`）；
- org 解析入口已证实（`authz/context.py:103`）；
- Runtime router 已挂载（`router_registry.py:88`），不需要改 Gateway 主干；
- 不需要新增 schema、migration、字段或第二状态源（幂等键桥接 + 自由 JSON input_payload）。

**A03 可通过局部代码处理**：
- `input_payload` 组装与 `task_id` 取值（传既有 Task Center 主任务 id）；
- org_id 从端点层传入 `run_app`（可选参数，向后兼容）；
- `result_summary` 提取与 `error` 脱敏（参照 `task_runtime_result` / `task_runtime_failure` 的 sanitize 模式）；
- `timed_out → failed` 投影；
- `waiting_approval` 不投影边界；
- Runtime 异常→入口错误路径：`run_app` 现有 `ValueError → HTTP 400`（`apps.py:509-510`）契约保持；bridge 失败抛可读 `ValueError`，**不静默 fallback 到旧链路**；opt-in 关闭时旧链路行为完全不变；
- 同步/异步：`run_app` 在 `asyncio.to_thread` 中执行，bridge 内用既有的 detached poll / `asyncio.run` 模式（参照 `_schedule_workflow_dispatch`，`app_runner.py:236-294`）。

**必须负责人决策**：无。A01 登记的三个未决项均可在上述"局部处理"边界内闭环（failed 回写由 bridge 主动回写 + 既有轮询作兜底，二者写点同为 `update_run_status`，无第二状态源）。

**触发硬停止条件**：未触发。不需要改 `qagent_runtime`、Gateway 主干、router registry、background startup、LangGraph 主链、前端；不需要新表/字段/migration。

## A03 允许修改范围复核

A01 登记的 5 个候选文件，本卡复核结论：

| # | 文件 | 复核结论 |
|---|---|---|
| 1 | `backend/packages/harness/evoflow/collab/app_runner.py` | 确认需要：`run_app_workflow` 创建 run 后接 opt-in bridge 触发；可增加可选 `org_id` 参数（向后兼容，automation 调用点不受影响） |
| 2 | `backend/app/gateway/app_runtime_bridge.py`（新建） | 确认需要：opt-in 开关、create/start 封装、幂等键 `app-run:{run_id}`、终态投影（completed/failed/cancelled/timed_out）、error 脱敏 |
| 3 | `backend/packages/harness/evoflow/persistence/app_repositories.py` | 可能不需要改动：终态回写复用现有 `update_run_status` / `load_run`；仅当桥接需要补充只读查询时才动，且不得改表结构 |
| 4 | `backend/app/gateway/routers/apps.py` | 确认需要（最小）：`run_app_endpoint` 解析 `org_id`（`resolve_request_principal`）并传给 `run_app`；请求/响应模型与权限语义不变 |
| 5 | `backend/app/tests/test_app_runtime_bridge.py`（新建） | 确认需要：幂等不二建、completed/failed 回写、取消语义、opt-in 关闭时旧行为不变 |

**结论：仍为 5 个（2 新建 + 3 既有），未超过 6 个，全部位于 A01 候选边界内。** 本卡未发现需要第 6 个文件的证据。
