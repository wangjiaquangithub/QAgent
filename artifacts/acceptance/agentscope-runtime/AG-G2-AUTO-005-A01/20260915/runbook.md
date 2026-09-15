# AG-G2-AUTO-005-A01 — Automation / Task Center 终态回写 真实 HTTP 验收 Runbook

- 卡片：`AG-G2-AUTO-005-A01`（母任务 `G2-AUTO-005`）
- 日期 / attempt：2026-09-15 / `20260915`
- 起始 HEAD：`cf57d5f1363c4f5df9c8729fb5e4f67ef896f0a7`（分支 `codex/agentscope-runtime`）
- 依赖：`AG-G2-AUTO-003-A01`（Runtime 终态/结果投影回 Task Center，提交 `327d2b0`）
- 环境与开关原始记录：`raw/environment.txt`
- 全部命令原始输出：`raw/`（29 个响应文件 + 1 个 PostgreSQL 事实源导出）

本 Runbook 只使用**既有** HTTP 入口，未新增任何 API、未改任何生产代码、测试、schema、migration 或配置。
`$BASE` = `http://127.0.0.1:8099`。

---

## 步骤 1：环境前置与基线

```bash
curl -s $BASE/health
curl -s $BASE/api/tasks
curl -s $BASE/api/tasks/queue/status
```

**预期**：Gateway 健康；任务列表为空；队列循环已启用。

**实际**（`raw/` 中对应文件）：

| 观察点 | 实际值 | 原始输出 |
|---|---|---|
| `/health` | `{"status":"healthy","service":"evo-flow-gateway"}` | 步骤 1 输出 |
| `/api/tasks` | `{"success":true,"data":{"tasks":[],"total":0,...}}` | `step01-list-tasks.json` |
| `/api/tasks/queue/status` | `backend_env_enabled=true`、`backend_loop_running=true`、`max_concurrent=3`、`tick_seconds=60`、`active_unattended=0` | `step01-queue-status.json` |

**注意（影响后续步骤的真实行为）**：`backend_loop_running=true` 表示 Gateway 自带一个 60s 的后台队列循环。
因此本 Runbook 里的 `POST /api/tasks/queue/tick` 是**手动触发同一段代码**（既有 debug 入口，`routers/tasks.py:932`），
而不是唯一驱动源；后台循环会在两次手动 tick 之间自行推进。步骤 8 的"零新增 Run"结论因此也经受住了后台循环的考验。

---

## 步骤 2：flag-on 创建 unattended 任务

`EVOFLOW_AUTOMATION_UNATTENDED_RUNTIME=1` 已设置。

```bash
curl -s -X POST $BASE/api/tasks -H 'Content-Type: application/json' \
  -d '{"name":"A05 终态回写验收","description":"AG-G2-AUTO-005-A01 真实 HTTP 验收","run_mode":"unattended"}'
```

**预期**：返回 `status=pending`、`execution_authorized=false`、`run_mode=unattended` 的任务行，且此时**不应**有 Runtime Run。

**实际**（`step02-create-task.json`）：

```
task_id = 2609151523_2481
status = pending        execution_authorized = false        run_mode = unattended
unattended_stage = queued        unattended_attempts = 0
queue_hint = "Task enqueued for unattended execution; Gateway task queue will pick it up."
```

未授权时直接调 `/start`（`step03-start.json`）得到
`{"advance":{"ok":false,"error":"thread_create_failed"}}` —— 未授权任务走旧 LangGraph 路径，符合既有语义。

---

## 步骤 3：flag-on 授权并启动 → 建立 Runtime Run

```bash
curl -s -X POST $BASE/api/tasks/2609151523_2481/authorize-execution -H 'Content-Type: application/json' -d '{}'
curl -s -X POST $BASE/api/tasks/2609151523_2481/start
```

**预期**：授权成功；`/start` 的 `advance.action == "runtime"` 并给出 `runtime_run_id`。

**实际**：

- `step04-authorize.json` → `execution_authorized=true`、`authorized_by=user`、`collab_phase=awaiting_exec`
- `step05-start-after-authorize.json` →
  `{"advance":{"ok":true,"action":"runtime","runtime_run_id":"run_3ef3edfc07c94b1f88be7f49cbfbcd8e","runtime_action":"attached"}}`

**task_id ↔ run_id 关联证据（两侧一致）**：

| 侧 | 观察 | 原始输出 |
|---|---|---|
| Task Center（SQLite `evoflow_collab_tasks.extra_json.runtime_run_linkage`） | `runtime_run_id=run_3ef3edfc07c94b1f88be7f49cbfbcd8e`、`org_scope_key=tc:org:local`、`task_id=2609151523_2481`、`idempotency_key=tc:587dfda4bcee531014e6003e6ea0d891` | `step07-sqlite-linkage.json` |
| Runtime（PostgreSQL `qagent_runs`） | `run_id=run_3ef3edfc07c94b1f88be7f49cbfbcd8e`、`task_id=tc-fe3607557b935eec37ce33a3`、`idempotency_key=tc:587dfda4bcee531014e6003e6ea0d891`、`status=queued` | `pg-runtime-truth.txt` |

**同一 task_id 的完整记录**（`GET /api/tasks/2609151523_2481`，`step06-task.json`）：
`status=pending`、`execution_authorized=true`、`execution_history=[]`（长度 0）。

**这一点必须写清楚**：Run 建立后任务回到 `pending`，`execution_history` 仍为空 ——
这正是 `AG-G2-AUTO-003-A01` 刻意采用的 reuse-only 边界（`_REUSE_REASONS = {"reused", "linked_run_terminal"}`）：
**创建分支不投影**，因为既有测试断言创建 attempt 后 `status == "planned"` 且历史为空。
投影只发生在"该 attempt 已有 Run 且又看到它"的时候，也就是步骤 4。

---

## 步骤 4：flag-on 队列 tick → Runtime 状态投影回 Task Center（A03 的核心）

```bash
curl -s -X POST $BASE/api/tasks/queue/tick
curl -s $BASE/api/tasks/2609151523_2481
curl -s $BASE/api/tasks/2609151523_2481/execution-history
curl -s $BASE/api/tasks/2609151523_2481/runtime
```

**预期**：tick 结果里同时出现"既有循环的 `already_scheduled`"与"新循环的 `runtime_projected`"；
`GET /api/tasks/{id}` 的 `execution_history` 出现 `source=qagent_runtime` 的记录。

**实际**（`step08-tick1.json`）：

```json
{"active":0,"slots":3,"picked":1,"results":[
  {"task_id":"2609151523_2481","ok":true,"action":"already_scheduled","runtime_run_id":"run_3ef3edfc07c94b1f88be7f49cbfbcd8e"},
  {"task_id":"2609151523_2481","ok":true,"action":"runtime_projected","runtime_run_id":"run_3ef3edfc07c94b1f88be7f49cbfbcd8e","runtime_projection":"unchanged","runtime_projection_reason":"duplicate_status"}
]}
```

- 第一条来自既有的候选/in-progress 循环：任务已关联 Run，故 `already_scheduled`，**不再建立第二个 Run**。
- 第二条来自 A03 新增的第三个循环（`list_unattended_runtime_linked`）：`action="runtime_projected"`。
  `runtime_projection="unchanged"` / `reason="duplicate_status"` 是因为 Run 当前 `queued` 对应的任务态就是 `pending`，与行内现值相同 —— 幂等而非无动作。

`GET /api/tasks/{id}`（`step08-task-after-tick.json`）的 `execution_history` 变为长度 1：

```json
[{"source":"qagent_runtime","runtime_run_id":"run_3ef3edfc07c94b1f88be7f49cbfbcd8e",
  "runtime_status":"queued","org_scope_key":"tc:org:local","approval_required":false}]
```

`GET /api/tasks/{id}/execution-history`（`step06-execution-history.json`）返回同一列表。

**必须记录的边界**：`GET /api/tasks/{task_id}/runtime`（`routers/tasks.py:2674`）是**既有的「任务运行时/agent 分配」视图**，
返回 `{"task_id":...,"status":"pending","agents":[],"updated_at":...}`（`step06-runtime.json`），
**不是** Runtime Run 的读出口。Run 的可读出口在 PostgreSQL 与上面的 `execution_history` 投影记录。

---

## 步骤 5：flag-on 取消 → Run 终态回写

```bash
curl -s -X POST $BASE/api/tasks/2609151523_2481/cancel
curl -s $BASE/api/tasks/2609151523_2481
```

**预期**：任务收敛为 `cancelled`，`execution_authorized` 撤销，`execution_history` 追加一条 `runtime_status=cancelled`；
PostgreSQL 中该 Run 变为 `cancelled`。

**实际**：

- `step10-cancel.json` → `{"success":true,"message":"Task cancelled","task_id":"2609151523_2481"}`
- `step10-task-after-cancel.json` → `status=cancelled`、`execution_authorized=false`，历史长度 2，末条：

```json
{"source":"qagent_runtime","runtime_run_id":"run_3ef3edfc07c94b1f88be7f49cbfbcd8e",
 "runtime_status":"cancelled","org_scope_key":"tc:org:local","approval_required":false,
 "reason":"cancellation requested through the runtime public boundary"}
```

- PostgreSQL（`pg-runtime-truth.txt`）：

```
run_3ef3edfc07c94b1f88be7f49cbfbcd8e | local | tc-fe3607557b935eec37ce33a3 | cancelled | ... | 2026-09-15 23:26:27.639208+08

sequence |     type      |          occurred_at
       1 | run.created   | 2026-09-15 23:24:27.43214+08
       2 | run.queued    | 2026-09-15 23:24:27.43214+08
       3 | run.cancelled | 2026-09-15 23:26:27.639208+08
```

**取消路径是本次验收中唯一完整走通的终态闭环**：既有取消入口 → Runtime 公开边界 `request_cancel` →
Run 落 `cancelled` 终态并写事件 → A03 投影把该终态写回 Task Center 任务行与 `execution_history`。

---

## 步骤 6：flag-on 二次 tick → 幂等与终态不倒退

```bash
curl -s -X POST $BASE/api/tasks/queue/tick
curl -s $BASE/api/tasks/2609151523_2481
```

**预期**：已终态的任务不再被拾取；历史不再增长；状态保持 `cancelled`。

**实际**（`step11-tick2.json`、`step11-task.json`）：

```json
{"active":0,"slots":3,"picked":0,"results":[]}
```

`status=cancelled`，`execution_history` 长度仍为 **2**（未追加第三条）。

即：终态任务被 A03 的第三个循环跳过，且既有循环也不再拾取 —— **重复 tick 幂等、终态不倒退**。

---

## 步骤 7：flag-on + Runtime 不可用 → fail-safe 降级（不伪造终态、不永久 running）

用第二个任务复现（`step13-*`）：创建 → 授权 → 启动，得到
`run_ffc16a9e6d38486e9e7341e0ce23e9a9`（task `2609151530_ac92`）。
随后把 Gateway 的 `QAGENT_POSTGRES_URL` 指向一个**不可用**端口（`127.0.0.1:5499`，本机无监听）并重启，
再执行一次 tick。

```bash
curl -s -X POST $BASE/api/tasks/queue/tick
curl -s $BASE/api/tasks/2609151530_ac92
```

**预期**：Runtime 读失败被吸收为 `runtime_unavailable`，任务**不**被写成 `completed`，也**不**永久停在 `executing`。

**实际**（`step14-tick-badpg.json`）：

```json
{"active":0,"slots":3,"picked":2,"results":[
  {"task_id":"2609151528_b0a7","ok":true,"action":"already_scheduled","runtime_run_id":"run_11485c43a1ba4317a0ce4dfebecaf2c2"},
  {"task_id":"2609151530_ac92","ok":true,"action":"already_scheduled","runtime_run_id":"run_ffc16a9e6d38486e9e7341e0ce23e9a9"},
  {"task_id":"2609151528_b0a7","ok":true,"action":"runtime_projected","runtime_run_id":"run_11485c43a1ba4317a0ce4dfebecaf2c2","runtime_projection":"runtime_unavailable","runtime_projection_reason":"runtime_unavailable"},
  {"task_id":"2609151530_ac92","ok":true,"action":"runtime_projected","runtime_run_id":"run_ffc16a9e6d38486e9e7341e0ce23e9a9","runtime_projection":"runtime_unavailable","runtime_projection_reason":"runtime_unavailable"}
]}
```

`step14-task.json` → `status=pending`，`execution_history` 长度 **0**（未追加任何伪造记录）。

**同一响应还暴露了一个此前未记录的边界**：`2609151528_b0a7` 是**步骤 8（flag-off）期间创建**的任务，
在 flag-off 期间它没有任何 linkage；以 flag-on 重启后，Gateway 的 60s 后台队列循环自动接管了它并建立了
`run_11485c43a1ba4317a0ce4dfebecaf2c2`。
即"关开关回退"不是"该任务永久免疫 Runtime"，而是"关开关期间不再新建 Run；重新开启后仍会被队列接管"。

---

## 步骤 8：flag-off 回退基线

不删除任何历史数据，仅移除开关（`env -u EVOFLOW_AUTOMATION_UNATTENDED_RUNTIME`）后重启 Gateway，
复用**同一个** `EVOFLOW_HOME`（即保留步骤 2~7 的全部 SQLite 记录），创建新任务
`2609151528_b0a7` 并重复授权 / 启动 / tick。

```bash
curl -s -X POST $BASE/api/tasks -H 'Content-Type: application/json' \
  -d '{"name":"A05 开关关闭基线","run_mode":"unattended"}'
curl -s -X POST $BASE/api/tasks/2609151528_b0a7/authorize-execution -H 'Content-Type: application/json' -d '{}'
curl -s -X POST $BASE/api/tasks/2609151528_b0a7/start
curl -s -X POST $BASE/api/tasks/queue/tick
```

**预期**：走旧路径；不建立 Runtime Run；不写 linkage；`execution_history` 不出现 `qagent_runtime`；PostgreSQL 计数不变。

**实际**：

| 观察点 | 实际值 | 原始输出 |
|---|---|---|
| `/start` 的 advance | `{"ok":false,"error":"thread_create_failed"}`（旧 LangGraph 路径） | `step12-start-off.json` |
| tick 结果 | `[{"task_id":"2609151528_b0a7","ok":false,"error":"thread_create_failed"}]`，**无** `runtime_projected` | `step12-tick-off.json` |
| 任务行 | `status=pending`，`execution_history=null`（长度 0） | `step12-task-off.json` |
| SQLite linkage | `runtime_run_linkage = null` | `step12-sqlite-off.json` |
| PostgreSQL Run 总数 | flag-off 前 **8** → flag-off 后 **8**（零新增） | `step12-pg-run-count-*.txt` |
| `idempotency_key like 'tc:%'` 计数 | flag-off 期间仍为 **1**（只有步骤 3 建立的那一条） | `step12-pg-tc-run-count.txt` |
| flag-off Gateway 日志中的投影调用 | **0** 命中 | `gateway-flagoff.log` grep |

**结论**：开关关闭即回到旧行为，且不产生任何 Runtime 副作用；历史数据（步骤 2~7 产生的 Run、linkage、历史记录）全部保留。

---

## 未覆盖项（必须在发布门槛中保留，勿误判为已验收）

| # | 未覆盖项 | 原因（本环境的真实阻塞） | 最小解除条件 |
|---|---|---|---|
| U1 | **Run 成功终态（`completed`）的端到端回写** | 无 `AGENTSCOPE_API_KEY` / `AGENTSCOPE_MODEL`；`provider_factory.py` 明确不提供 fake provider 回退。且无人值守路径**只调 `establish_runtime_run`（create_run），从不调 `start_run`**（`unattended_task_pipeline.py` 仅 606/621/797 三处，无 start 调用），Run 会一直停在 `queued`。 | 配置真实 OpenAI 兼容 provider 凭据（`AGENTSCOPE_MODEL` + `AGENTSCOPE_API_KEY` + 可选 `AGENTSCOPE_ENDPOINT`），并提供一个合法的、可驱动 unattended Run 执行的入口。 |
| U2 | **Run 失败终态（`failed` / `timed_out`）的端到端回写** | 同上。此外已确认 `POST /api/qagent/runtime/runs/{run_id}/start` **无法**驱动本卡建立的 Run：该入口需要 bearer token（本环境 401，见 `step09-start-run.json`），且其 `principal.org_id` 为 `identity:<type>:<id>`，与 Run 的 `org_id="local"` 不匹配，`service._require_run` 会拒绝。 | 先修 `org_id` 缺口（见下 P1），或提供一个以 `local` 为 org 的既有驱动入口。 |
| U3 | **Event 流式续读** | 本卡只读终态与既有读取出口，未使用 `GET /api/qagent/runtime/runs/{id}/events`；SSE 续读属 Event v1 范围。 | 由 Event v1 / `G0-DEC-003` 冻结后另派卡。 |
| U4 | **事件 gap** | 同上；本卡只观察到 `sequence = 1,2,3` 连续无缺口，未构造缺口场景。 | `G0-DEC-003`。 |
| U5 | **跨实例幂等** | 单实例本地 Gateway；`max_concurrent=3` 是单进程并发上限，不构成多实例一致性证据。 | 多实例环境 + `G0-DEC-002`。 |
| U6 | **两套入口的组织命名空间未统一** | 无人值守路径建立的 Run `org_id = "local"`（`pg-runtime-truth.txt` 三条 Run 全部为 `local`）。该值由可信 `AuthzContext` 显式传入、语义正确：`establish_runtime_run` 传 `org_id=context.org_id`（`task_runtime_optin.py:427`），`context.org_id` 来自 `_trusted_org_id`（`task_runtime_context.py:182`，缺 org 即拒绝、不自造默认），单机部署下 `build_authz_context` 填 `DEFAULT_ORG_ID="local"`（`evoflow/authz/types.py:13`）。阻塞在于 Runtime 原生入口的 principal org 为 `identity:<type>:<id>`（`auth.py:34-36`），两者不同名，使该入口无法驱动 Task Center 建立的 Run。注：`manual-acceptance.md` §9 的 P1「无人值守路径调用 Runtime 时未传组织」描述**已过时**（已由 `AG-G2-AUTO-008` 修复）。 | 统一两套入口的组织命名空间，或提供一个以 Task Center 可信组织为 org 的既有驱动入口。 |
| U7 | **页面路径** | 卡片要求"HTTP / 页面"；本环境未启动 evopanel（Tauri + Vite），只走了 HTTP。页面路径的等价性未验证。 | 启动前端并以同一任务复跑步骤 2~6。 |

## 回滚方式

只关开关（步骤 8 已验证），**不删除任何历史数据**：已产生的 `qagent_runs` / `qagent_run_events`、
任务行上的 `runtime_run_linkage`、`execution_history` 记录全部保留。
本卡未改任何生产代码，因此不存在代码回滚动作。
