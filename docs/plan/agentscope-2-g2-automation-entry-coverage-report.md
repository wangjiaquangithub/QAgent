# G2 Automation / Task Center 域：真实入口覆盖报告（AG-G2-AUTO-039 → 045）

- 卡片：`AG-G2-AUTO-039` → `AG-G2-AUTO-045`（008–038 队列之后的续队列）
- 分支：`codex/g2-automation-task-center`
- 起点：`0fa8e4a`
- 用途：把**真实 HTTP / 调度入口**上的 Runtime 行为一次列清 —— 每张卡覆盖了什么、由哪个测试文件固化、以及本次回归发现的**真实缺口**与已固化的边界。避免后续验收时把「单元级已覆盖」误当成「真实入口已覆盖」。

---

## 1. 覆盖矩阵

全部走**既有入口**，不新增路由、不改前端、不碰 `qagent_runtime` / migrations / LangGraph 主链。

| 卡片 | 覆盖的真实入口 | 测试文件 | 测试数 | commit |
|---|---|---|---|---|
| 039 | `POST /api/tasks/{id}/authorize-execution` + `POST /api/tasks/{id}/start`（手动 run-now） | `test_task_runtime_manual_entry_flow.py` | 20 | `c912030` |
| 040 | 尝试级重试（`POST /api/tasks/queue/tick` 重排）vs 子任务重试（`POST /api/tasks/{id}/retry`） | `test_task_runtime_retry_entry_flow.py` | 18 | `a438932` |
| 041 | `POST /api/tasks/{id}/cancel` 及其后的手动触发（dispatch / run-now） | `test_task_runtime_cancel_entry_flow.py` | 13 | `468f80c` |
| 042 | `POST /api/automation/tasks/{id}/run`（规则手动运行） | `test_task_runtime_automation_entry_flow.py` | 15 | `0fa8e4a` |
| 043 | `POST /api/tasks/queue/tick` + `GET /api/tasks/queue/status`（调度器 pickup） | `test_task_runtime_scheduler_entry_flow.py` | 13 | `fd2abfc` |
| 044 | 文档：可复制的 API 验收序列 | `docs/task-center-runtime-manual-acceptance.md`、`backend/app/tests/TASK_RUNTIME_TESTING.md` | — | `7044c9f` |
| 045 | 回归收尾 + 本报告 | 本文件 | — | — |

每张卡独立提交、独立 push，各自跑 scoped 测试 + scoped ruff（`ruff check`，不跑 `format`）。

---

## 2. 真实缺口与已固化的边界

### 2.1 automation 规则 run-now 到 Task Center 的**可信身份断链**（P1，未修，已测试固化）

`_run_one_task` 的 opt-in 分流（`app/gateway/automation_runner.py`）：

```
app_id 绑定            → _run_bound_app_for_automation（App Runner，先于开关判定，不受影响）
prompt-only + opt-in   → _enqueue_automation_as_unattended_task + _kick_unattended_task
其余（默认）           → 原 LangGraph 直连路径，逐字节不变
```

opt-in 打开后，会创建一条 Task Center 无人值守任务（`run_mode=unattended`、`execution_authorized=True`、`raised_by=automation`）。**但该任务没有写入 root task 的 owner scope**，因此 `resolve_server_task_runtime_identity(task_id)` 返回 `None`，`_maybe_run_via_runtime` 走 `trusted_identity_unavailable` 的 legacy 分支 —— 即：**任务被创建了，Runtime Run 没有被建立**。

- 固化测试：`test_the_kick_without_a_trusted_identity_defers_to_the_legacy_path`（断言 `decision=legacy`、`reason=trusted_identity_unavailable`）；身份齐备时全链路的正向测试用 `_give_the_task_a_trusted_identity` 显式写入 owner scope 后跑通。
- 未修原因：为「规则触发的任务」确定 `created_by`（即这条无人值守任务归谁）是**身份归属决策**，牵动授权与归属模型，超出本队列的自主范围。`automation_repositories` 已有 `set_automation_owner_scope` / `get_automation_owner_scope`，但未向被创建任务传播。
- 影响面：这是一个**安全失败**（fail-safe），不会误建 Run、不会串组织；但「规则 → 无人值守 Runtime」这条链路目前在真实环境里尚未真正打通。

### 2.2 run-now 与 tick 是**两条不同**的重试入口（已固化，非缺陷）

- 队列 tick 不预先改动任务状态，所以终态任务仍会进入管线的重排分支 → `requeued_for_retry`（新 attempt）。
- run-now（`POST /{id}/start`）先把 `failed` 移出终态再推进，因此**够不到**重排分支，Runtime 返回既有 Run（`reused_terminal_run`）。安全（不建第二个 Run、不吞掉旧链路），但**尝试级重试的真实入口是队列，不是 run-now**。
- 且队列的候选集只收 `pending`（以及 backoff 到期的 failed），所以重排后的新 attempt 由 run-now 启动，而不是第二个 tick。
- 均由 `test_task_runtime_retry_entry_flow.py` 固化。

### 2.3 调度器语义（已固化）

- 同一触发窗口重复 tick **不会**为同一 attempt 建第二个 Run：`decide_runtime_pickup` 读到持久化 linkage 即报 `already_scheduled` 并跳过；答案来自任务行而非进程内状态，所以**不需要也不需要 leader / lease / fencing**（第二实例 / 重启后读同一存储得到同一答案，已测试固化）。
- 两个开关互不污染：`EVOFLOW_TASK_QUEUE_ENABLED` 只管队列本身（关掉即 `{"skipped": true, "reason": "disabled"}`），`EVOFLOW_AUTOMATION_UNATTENDED_RUNTIME` 只管 opt-in；默认关闭时 tick 照旧把任务交给旧管线。

---

## 3. 回归（回填见 §3.1）

```bash
cd backend
PYTHONPATH=".:packages/harness" python -m pytest app/tests/test_task_runtime_*.py -q
uvx ruff check <changed files>
```

注意：必须用带 FastAPI 的项目虚拟环境跑，否则 `*_flow.py` 等真实路由测试会**自我跳过**（而不是失败），回归就不是回归。

### 3.1 本次结果

```
677 passed in 1572.49s (0:26:12)
```

- 用带 FastAPI 的项目虚拟环境跑，`*_flow.py` 全部真实执行（不跳过）；
- scoped ruff：改动文件 `All checks passed!`；
- 本轮修掉 2 处由「缓存淘汰」暴露的既有断言脆弱性（见 §4），未改生产代码。

> 耗时说明：同一个测试集在本机先后两次全量跑的墙钟时间差异很大（4 分 09 秒 / 26 分 12 秒），与用例数量无关（两次都是 677 项）。本域的 `queue/tick` 是**全局**扫描（遍历存储里每一个无人值守任务），而存储在一个进程内会随用例累积，因此这套回归本身偏重、且对环境负载敏感。若只需回归本轮改动，跑 `app/tests/test_task_runtime_*_flow.py` 即可，快得多。

---

## 4. 本次修正的既有测试脆弱性（非本次新增代码引起）

回归首次全量跑出现 2 处 `KeyError: 'execution_history'`，且**只在全量跑出现、单文件跑通过**。

- 根因（已用探针证实）：`ProjectStorage` 有**进程内 LRU 项目缓存**（上限 64 条、TTL 30 秒）。任务刚被保存时，`load_project` 命中缓存，返回的就是内存里那份 dict，因此带 `execution_history: []`；一旦缓存被淘汰，读回落回行映射器，而 `execution_history` 为**空**时不会落键（`if task_hist:` 才写），于是直接下标 `row[HISTORY_FIELD]` 抛 `KeyError`。
- 即：`assert row[HISTORY_FIELD] == []` 这类断言**依赖缓存是否命中**，本身是脆弱的。
- 修正：改用支撑模块既有的 `history_of(row)`（键缺失时返回 `[]`），与缓存状态无关。
  - `test_task_runtime_manual_entry_flow.py`（本次新增卡 039 的文件）；
  - `test_task_runtime_unattended_success.py`（**改动前既有**文件，同类断言，同因暴露）。
- 仅改断言写法与一处导入，未改任何生产代码。

---

## 5. 范围声明

本轮全程未改动：`backend/app/qagent_runtime/`、`backend/migrations/`、LangGraph 主链与 `/api/langgraph`、Gateway 主干与 `router_registry.py` / `background_startup.py`、Chat / Live-Run 域、全局 SSE、App Runner、设备协议与 lease/fencing、前端、依赖与 lockfile。

新增测试与文档全部落在本域既有目录：`backend/app/tests/`、`backend/app/tests/TASK_RUNTIME_TESTING.md`、`docs/task-center-runtime-manual-acceptance.md`、`docs/plan/`。
