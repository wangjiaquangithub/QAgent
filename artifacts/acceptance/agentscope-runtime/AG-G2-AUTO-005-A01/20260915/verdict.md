# AG-G2-AUTO-005-A01 — verdict

- 卡片：`AG-G2-AUTO-005-A01`（母任务 `G2-AUTO-005`，真实 HTTP / 页面手工验收 Runbook）
- 日期 / attempt：2026-09-15 / `20260915`
- 起始 HEAD：`cf57d5f1363c4f5df9c8729fb5e4f67ef896f0a7`
- 验收命令（卡片唯一标准）：`test -s artifacts/acceptance/agentscope-runtime/AG-G2-AUTO-005-A01/20260915/verdict.md`
- 产物：`runbook.md`（8 步，每步含预期与实际）、`raw/`（29 个原始 HTTP 响应 + PostgreSQL 事实源导出 + 环境记录）

## 结论：`Blocked`（3 个目标终态中完成 1 个）

真实 Gateway 与真实 PostgreSQL 均可用，**取消终态**的端到端回写闭环已完整走通并有原始输出佐证；
**成功终态与失败终态**在本环境不可达，触发卡片停止条件「真实 Gateway / PostgreSQL / Provider 不可用」。

| 卡片目标 | 判定 | 依据 |
|---|---|---|
| 一次**成功**（`completed`）端到端验收 | **Blocked** | 无 `AGENTSCOPE_*` provider 凭据；且无人值守路径不驱动 Run 执行 |
| 一次**失败**（`failed` / `timed_out`）端到端验收 | **Blocked** | 同上；另经实测 Runtime 原生 start 入口无法驱动该 Run |
| 一次**取消**（`cancelled`）端到端验收 | **通过** | `raw/step10-*`、`raw/step11-*`、`raw/pg-runtime-truth.txt` |
| 开关关闭旧行为 + 回退 | **通过** | `raw/step12-*`（PG Run 数 8 → 8，零新增） |
| Runtime 读失败 fail-safe | **通过** | `raw/step14-*`（`runtime_unavailable`，任务未被伪造为终态，未永久 `executing`） |

## 已通过项的关键证据（摘要）

**取消闭环（A03 成果在真实 HTTP 路径上生效）**

| 环节 | 观察 |
|---|---|
| 既有入口建立 Run | `POST /api/tasks/{id}/start` → `{"ok":true,"action":"runtime","runtime_run_id":"run_3ef3edfc07c94b1f88be7f49cbfbcd8e","runtime_action":"attached"}` |
| 队列 tick 投影 | `POST /api/tasks/queue/tick` → 结果里同时出现 `already_scheduled`（既有循环）与 `runtime_projected`（A03 新增循环） |
| 历史回写 | `GET /api/tasks/{id}` 的 `execution_history` 出现 `{"source":"qagent_runtime","runtime_run_id":"run_3ef3...","runtime_status":"queued",...}` |
| 取消 | `POST /api/tasks/{id}/cancel` → `{"success":true,"message":"Task cancelled"}` |
| Runtime 侧终态 | `qagent_runs.status = cancelled`；事件 `run.created(1) → run.queued(2) → run.cancelled(3)` |
| Task Center 侧终态 | `status = cancelled`，`execution_authorized = false`，历史追加 `runtime_status=cancelled` |
| 幂等 / 终态不倒退 | 二次 tick → `{"picked":0,"results":[]}`，历史长度保持 2 |
| 两侧关联一致 | SQLite `runtime_run_linkage.idempotency_key = tc:587dfda4bcee531014e6003e6ea0d891` = PG `qagent_runs.idempotency_key` |

**fail-safe（步骤 7）**：把 `QAGENT_POSTGRES_URL` 指向无监听端口后 tick，两个已关联 Run 的任务都返回
`runtime_projection="runtime_unavailable"`；任务保持 `pending`，`execution_history` 长度 0 —— 未伪造 `completed`，未永久 `executing`。

## 阻塞原因（两条独立硬阻塞，均已实测确认）

**B1 — 无真实 Provider 凭据。**
`AGENTSCOPE_PROVIDER` / `AGENTSCOPE_MODEL` / `AGENTSCOPE_API_KEY` / `AGENTSCOPE_ENDPOINT` 在本环境全部未设置。
`backend/app/qagent_runtime/provider_factory.py:38-41` 在缺 `AGENTSCOPE_MODEL` / `AGENTSCOPE_API_KEY` 时抛
`ProviderConfigurationError`，且该文件第 3 行显式声明运行时不提供 fake/deterministic provider 回退。
因此 Run 一旦真正执行必然失败，成功终态无从产生；失败终态也无法与"配置缺失"区分。

**B2 — 无人值守路径不驱动 Run 执行，而既有驱动入口无法作用于该 Run。**
`backend/app/gateway/unattended_task_pipeline.py` 中与 Runtime 相关的调用只有 606 / 621 / 797 三处，
全部围绕 `establish_runtime_run`（即 `create_run`）；**没有任何 `start_run` 调用**。
Run 建立后稳定停在 `queued`（`raw/pg-runtime-truth.txt` 中三条 `tc:` Run 的 status 分别为 `cancelled` / `queued` / `queued`）。

唯一能驱动执行的既有 HTTP 入口是 `POST /api/qagent/runtime/runs/{run_id}/start`
（`backend/app/qagent_runtime/router.py:64`），实测两条独立阻碍：

1. 需要 `Authorization: Bearer <token>`，本环境无有效 token → `401 Missing or malformed Authorization header`（`raw/step09-start-run.json`）。
2. 即便有 token 也不成立：该入口的 `principal.org_id` 形如 `identity:<type>:<id>`（`auth.py:34-36`），
   而无人值守路径建立的 Run `org_id = "local"`（`service.create_run` 的默认值），
   `service._require_run(run_id, org_id=...)` 会因组织不匹配而拒绝。

第 2 条使 `docs/plan/agentscope-2-g2-automation-manual-acceptance.md` §9 登记的 **P1 缺口**
（无人值守路径调用 Runtime 时未传组织）后果扩大：它不只让 Run 的 `org_id` 归属不正确，
还**阻断了从既有 HTTP 入口驱动该 Run 执行**，因而直接阻塞本卡的 U1/U2。

## 最小解除条件

| 阻塞 | 解除条件 |
|---|---|
| B1 | 配置真实 OpenAI 兼容 provider 凭据：`AGENTSCOPE_MODEL` + `AGENTSCOPE_API_KEY`（可选 `AGENTSCOPE_ENDPOINT`、`AGENTSCOPE_TIMEOUT`）。凭据由部署环境注入，不得写入仓库或本产物。 |
| B2 | 二选一：(a) 先在 `RuntimeRunContract` 协议中声明 `org_id` 并在无人值守调用处传入可信组织值，使 Runtime 原生入口可按组织驱动该 Run；(b) 或提供一个以 `local` 为 org 的既有执行驱动入口。任一项都属契约/状态机级改动，**须先由负责人拍板**，不得由本卡自行实现。 |

解除后本卡可重派，届时按 `runbook.md` 步骤 2~6 的同一路径复跑，把步骤 7 的 fail-safe 探针换成真实 provider，
补齐 U1/U2 的原始输出即可。

## 未覆盖项（完整清单见 `runbook.md` 末节）

U1 成功终态 · U2 失败终态 · U3 Event 流式续读 · U4 事件 gap · U5 跨实例幂等 · U6 `org_id` 归属（P1）· U7 页面路径。
其中 U3~U5 属 `G0-DEC-002` / `G0-DEC-003` 范围；U6 属 P1 缺口；U7 因未启动前端而未验证。

## 本卡是否产生代码改动

**否**。只写了 `artifacts/acceptance/agentscope-runtime/AG-G2-AUTO-005-A01/20260915/` 下的 `runbook.md`、
`verdict.md` 与 `raw/`。未触碰任何生产代码、测试、schema、migration、配置或前端；
未创建或删除 branch / worktree；未执行任何被任务包禁止的 git 操作。
