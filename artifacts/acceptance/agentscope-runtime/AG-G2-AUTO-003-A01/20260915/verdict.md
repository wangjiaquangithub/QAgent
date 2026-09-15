# AG-G2-AUTO-003-A01 — 终态回写与既有读取出口闭环

- 卡片：`AG-G2-AUTO-003-A01`（母任务 `G2-AUTO-003`，阶段 G2，Owner Domain Migration + Runtime）
- 动作类型：实现（最小接线 diff + 1 个聚焦测试）
- 工作目录 / 分支：`/Users/wangjiaquan/project/QAgent-agentscope-runtime` / `codex/agentscope-runtime`
- 起始 HEAD：`4f27a910670f26ec7868bf667ef519f14ca30dba`
- attempt：`20260915`
- 上游：`AG-G2-AUTO-001-A01`（inventory）、`AG-G2-AUTO-002-A01`（mapping）

---

## 1. 改动文件

| 文件 | 性质 | 说明 |
| --- | --- | --- |
| `backend/app/gateway/unattended_task_pipeline.py` | 生产（既有收敛点） | 新增 `_current_task_row`、`project_linked_runtime_state`、`list_unattended_runtime_linked`；在 `_maybe_run_via_maybe` 的 **reuse** 分支追加一次投影 |
| `backend/app/gateway/task_queue_runner.py` | 生产（队列 tick） | `task_queue_tick` 末尾追加一个「已关联 Runtime Run」的投影循环 |
| `backend/app/tests/test_task_runtime_terminal_writeback.py` | 新增测试 | 15 个用例 |
| `backend/app/tests/test_task_runtime_recovery_flow.py` | **既有测试，1 个用例的断言位置调整**（见 §4） | — |

未触碰：`backend/app/qagent_runtime/`、`backend/migrations/`、`routers/tasks.py`（读取出口已存在）、`task_runtime_*` 领域模块、schema、配置、前端。

## 2. 接线点与语义

### 2.1 复用哪些已存在的能力（全部只读）

- `task_runtime_optin.default_runtime_contract()` → `RuntimeService.get_run_status(run_id)`
- `task_runtime_degrade.reconcile_task_runtime_safely` → 内部走 `task_runtime_reconcile.reconcile_linked_task_runtime` → `task_runtime_projection.project_runtime_status` + `task_runtime_cursor.advance_runtime_cursor`
- `task_runtime_result.sanitize_result_summary` / `task_runtime_failure.sanitize_*`（经投影层调用）

**不新建 Run**：整个回写路径没有任何 `create_run` / `start_run` / `resume_run` / `request_cancel` 调用。

### 2.2 为什么需要 tick 里的第三个循环

`list_unattended_candidates` / `list_unattended_in_progress` 都按任务 `status` 取行；Run 关联之后该 status 由 Runtime 决定。因此一个「已关联 Run」的任务可能同时不在两个列表里（典型：Run 报 `running` → 任务 `executing`、无子任务、且仍有执行授权），此时没有任何既有循环会再问 Runtime 它后来怎么样了 —— 任务会永久停在 `executing`。`list_unattended_runtime_linked()` 只回答这一个问题（从持久化 linkage 判断），第三个循环据此投影。

### 2.3 开关与不变式

- 总闸：`EVOFLOW_AUTOMATION_UNATTENDED_RUNTIME`（默认关）。关闭时 `list_unattended_runtime_linked()` 直接返回 `[]`（不扫描），`project_linked_runtime_state()` 直接返回 `None`。
- 未关联任务：不在 `list_unattended_runtime_linked()` 结果中；`task_queue_tick` 对它们的行为逐字节不变（原有 skip / advance 分支未改）。
- 身份与组织：`task_runtime_optin.resolve_server_task_runtime_identity` 从任务行持久化 ACL 列解析；解析不出即跳过，不猜默认值。
- 执行授权：取服务端**真实**观察值 `is_task_execution_authorized(...)`，不假定 `True`。
- 失败兜底：`reconcile_task_runtime_safely` 把坏链路 / Runtime 读失败吸收为 `runtime_unavailable`，任务行保持不变，不伪造 `completed`。

## 3. 验收命令与原始结果

```bash
cd backend && PYTHONPATH=. .venv/bin/pytest -q app/tests/test_task_runtime_terminal_writeback.py app/tests/test_task_runtime_api_reads.py
```
原始输出见 `acceptance.txt`：**`26 passed in 16.32s`**。

### 3.1 聚焦测试覆盖（`test_task_runtime_terminal_writeback.py`）

| 用例 | 断言 |
| --- | --- |
| `test_a_completed_run_settles_the_task_as_completed` | `completed` → 任务 `completed` + history `completed` |
| `test_a_failed_run_settles_the_task_as_failed` | `failed` → 任务 `failed` |
| `test_a_timed_out_run_settles_the_task_as_failed_and_keeps_the_runtime_word` | `timed_out` → 任务 `failed`，history 保留 `timed_out` |
| `test_a_cancelled_run_settles_the_task_as_cancelled` | `cancelled` → 任务 `cancelled` |
| `test_the_settled_task_reads_back_through_the_real_routes` | `GET /api/tasks/{id}` + `/execution-history` 可读，且不泄漏 `runtime_run_linkage` |
| `test_a_task_in_neither_queue_list_is_still_reached` | **本卡存在的理由**：任务先被投影为 `executing`，断言它确实不在 `list_unattended_candidates()` 与 `list_unattended_in_progress()` 中，随后 tick 仍能把它收敛到 `completed` |
| `test_the_running_stage_is_projected_once_and_not_repeated` | 重复 tick 不产生第二条记录 |
| `test_a_settled_task_is_not_reopened_by_a_later_run_state` | 终态不倒退 |
| `test_a_settled_task_is_left_to_the_existing_retry_semantics` | 终态任务的 `unattended_attempts` 不被投影改动 |
| `test_the_projection_never_creates_a_second_run` | `create_calls == 1`，且确有 `get_run_status` 读取 |
| `test_an_unlinked_task_is_not_projected` | 未关联任务零 Runtime 调用、history 为空 |
| `test_with_the_switch_off_the_linked_sweep_is_empty` | 开关关闭：扫描结果为空、任务行逐字段不变、零 Runtime 调用 |
| `test_with_the_switch_off_the_tick_projects_nothing` | 开关关闭：tick 不投影、零 Runtime 调用 |
| `test_the_manual_run_now_control_also_projects_the_run` | `POST /api/tasks/{id}/start` 的 reuse 分支同样收敛（`runtime_projection == "reconciled"`） |

### 3.2 既有回归（原始输出见 `regression.txt`）

```bash
cd backend && PYTHONPATH=. .venv/bin/pytest -q app/tests/test_task_runtime_*.py app/tests/test_qagent_runtime*.py
```
结果：**`735 passed in 325.63s`**（exit=0）。

`ruff check` 对四个改动文件全部通过。

## 4. ⚠️ 超出卡片写域的 1 处改动（需负责人确认）

- **文件**：`backend/app/tests/test_task_runtime_recovery_flow.py`
- **用例**：`test_the_queue_does_not_re_pick_a_task_that_already_has_a_run`
- **原因**：该用例在 tick **之后**断言 `decide_runtime_pickup(stored_task(...)).action == "already_scheduled"`。本卡让 tick 把已关联 Run 的终态投影回任务行，该任务因此被合法收敛为 `completed`，`decide_runtime_pickup` 于是返回 `task_settled`。这是卡片验收标准明确允许的变化（「未关联任务与开关关闭时行为逐字节不变」，即已关联任务的行为可以变）。
- **处理**：把原来的三条断言**原样保留**、前移到 tick 之前（即「tick 拿到的行」上，这才是持久化 linkage 守卫可观察的位置），并在 tick 之后补上「任务被 Run 自身答案收敛、且 tick 没有重新拾取、没有创建任何 Run」的断言。**没有删除、跳过或放宽任何断言**。
- **为什么不改设计绕开**：任何「让 tick 把 Run 终态写回任务行」的实现都会命中这一条；不改则该卡目标（既有 API 读到 Runtime 结果）无法达成，A04 / A05 也无法进行。
- **范围**：仅此一个用例，25 行 diff（其中 20 行为注释与新增断言）。

## 5. 未覆盖 / 已知缺口（本卡明确不做）

| 项 | 说明 |
| --- | --- |
| Run 的启动 | 本卡不调用 `start_run`。unattended 路径建立 Run 后 Run 停在 `created`；真正执行需由既有 Runtime HTTP 入口启动（`POST /api/runtime/runs/{run_id}/start`）。这是既有 Run 建立路径（`433a9df`）的范围，本卡不重派、不扩权。 |
| 撤回执行授权后的收敛 | 执行授权被撤回（`revoke-execution-authorization`）时，可信上下文构建失败 → 跳过投影，任务保持现状。属保守选择：不伪造上下文，也不伪造终态。 |
| Event 流式帧 / cursor / gap | 未引入（`G0-DEC-003`）。 |
| cancel 与 approval 耦合 | 未引入（`G0-DEC-001`）。 |
| 跨实例触发幂等 / claim / lease / fencing | 未引入（`G0-DEC-002`）。 |
| 重启后自动再投影 | 本卡的 tick 循环是稳态收敛，不持有跨重启账本、不写对账记录；重启对账仍属未实例化（`G0-DEC-004`）。 |
| 前端 | 未改动。 |

## 6. 停止条件核对

卡片列出的停止条件（需改 `qagent_runtime` / schema / migration；需新增 `TaskStatus` 值或新表；需 Event v1 流式帧、cursor、gap；需跨实例幂等；需改前端或 LangGraph 主链）**均未触发**。
