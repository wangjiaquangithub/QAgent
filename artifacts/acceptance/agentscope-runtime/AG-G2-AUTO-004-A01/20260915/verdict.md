# AG-G2-AUTO-004-A01 — 终态回写路径的幂等、失败与终态不倒退定向测试

- 卡片：`AG-G2-AUTO-004-A01`（母任务 `G2-AUTO-004`，阶段 G2，Owner Domain Migration）
- 动作类型：测试（**只新增 1 个测试文件，零生产代码改动**）
- 依赖：`AG-G2-AUTO-003-A01`
- 工作目录 / 分支：`/Users/wangjiaquan/project/QAgent-agentscope-runtime` / `codex/agentscope-runtime`
- 起始 HEAD：`327d2b0b5f276f8459cf340941a1f0cc80966314`
- attempt：`20260915`

---

## 1. 改动文件

| 文件 | 性质 |
| --- | --- |
| `backend/app/tests/test_task_runtime_writeback_boundaries.py` | **新增**（唯一产物） |

`git status --short` 证明：本次提交不含任何生产代码、schema、migration、配置或既有测试改动。

复用（只读）：`backend/app/tests/_runtime_flow_support.py`。

## 2. 验收命令与原始结果

```bash
cd backend && PYTHONPATH=. .venv/bin/pytest -q app/tests/test_task_runtime_writeback_boundaries.py
```
原始输出见 `acceptance.txt`：**`21 passed in 19.69s`**。`ruff check` 通过。

## 3. 覆盖矩阵（卡片「必须覆盖」逐项对应）

| 卡片要求 | 用例 | 结果 |
| --- | --- | --- |
| `running` | `test_each_settled_stage_lands_on_the_frozen_mapping[running-executing]` | 任务 `executing` |
| `completed` | 同上 `[completed-completed]` | 任务 `completed` |
| `failed` | 同上 `[failed-failed]` | 任务 `failed` |
| `cancelled` | 同上 `[cancelled-cancelled]` | 任务 `cancelled` |
| `timed_out` | `test_timed_out_converges_the_task_to_failed_and_keeps_its_own_word` | 任务 `failed`，history 保留 `timed_out` |
| 重复 tick 幂等 | `test_a_repeated_tick_for_the_same_stage_writes_nothing_again`（4 个状态各一次）+ `test_a_repeated_terminal_tick_does_not_append_a_second_terminal_record` | 不产生第二条记录 |
| 终态不倒退 | `test_a_later_terminal_never_overwrites_an_earlier_one`（`failed` / `cancelled` / `timed_out` 三种后续终态均被拒） | 状态与 history 均不变 |
| 乱序 / 迟到状态 | `test_a_late_older_status_does_not_move_the_task_backwards`（回写路径可被喂到的乱序形态：更早阶段的状态重读） | 被拒，任务不回退 |
| 迟到 sequence | `test_an_older_sequence_is_refused_at_the_projection_boundary` | `stale / older_sequence` |
| `failed` / `timed_out` 错误摘要经既有脱敏函数 | `test_a_failure_summary_is_redacted_at_the_projection_boundary` | `error_code` 规范化保留；泄漏型 message 被整条丢弃并置 `error_detail_redacted=true`；落库 JSON 无 `/srv/app`、无 `Traceback` |
| 回写路径不得落任何 provider 载荷 | `test_the_write_back_path_never_stores_a_provider_payload` | 带 `error.message`（traceback + 路径）与 `result.internal_path` 的 Runtime 应答，落库行内均不可见 |
| `runtime_run_linkage` 不可解码不得当作「无 Run」 | `test_an_undecodable_linkage_is_not_treated_as_an_unlinked_task` + `test_a_linkage_missing_a_field_is_also_refused` | **不创建第二个 Run**；上报 `runtime_unavailable / linkage_malformed`；不外泄 run id |
| Runtime 查询失败不能永久 running | `test_an_unreadable_runtime_neither_fabricates_nor_strands_the_task` | 失败不伪造终态、任务行不变；恢复后下一次 tick 收敛到 `completed` |
| 失败可重试性 | `test_an_unavailable_runtime_is_reported_as_retryable` | `is_retryable_reason(...) is True` |
| 开关关闭零 Runtime 调用 | `test_with_the_switch_off_no_runtime_call_happens_and_nothing_is_written` | 任务行逐字段不变，`create` / `status` 调用数不变 |
| 回写路径不得创建 Run | `test_the_write_back_path_cannot_create_a_run_even_when_it_fails` | 失败态下 `create_calls == 0`，`cancel_calls == []` |

## 4. 未覆盖 / 已知缺口（明确登记，未以放宽断言掩盖）

| 缺口 | 说明 | 影响 |
| --- | --- | --- |
| 回写路径不携带 error / result 载荷 | `task_runtime_reconcile.reconcile_linked_task_runtime`（本卡不可改）只向 `project_runtime_status` 传 `runtime_status` 与 `expected_run_id`，不传 `error_code` / `reason` / `result_summary`。因此 tick 回写出的 `failed` / `timed_out` 记录**没有** `error_code`，`completed` 记录的 `result_available` 为 `false` | 与 `docs/plan/agentscope-2-g2-automation-completion-checklist.md` §1.4「`timed_out` 保留脱敏 `error_code` / `reason`」不完全一致。脱敏能力本身已存在并在**事件帧路径**（`task_runtime_event_bridge.apply_runtime_event`）上生效；本卡按「不新增产品功能」要求，只在投影边界（回写路径实际调用的那个函数）上钉住脱敏行为，不修 reconcile 的载荷传递 |
| `cancel` 与 approval 耦合子场景 | 卡片要求标记 `Blocked by G0-DEC-001` | 本卡未断言该子场景终态语义（无相关用例） |
| 撤回执行授权后的收敛 | 可信上下文构建失败 → 跳过投影（A03 的保守设计） | 未覆盖 |
| Event v1 流式续读 / cursor / gap、跨实例幂等 | 属 `G0-DEC-003` / `G0-DEC-002` | 未覆盖 |

## 5. 停止条件核对

卡片停止条件：「需要修改生产代码才能通过时立即停止并回报，不得为通过测试放宽或删除断言」——**未触发**：全程零生产代码改动，21 个用例全通过，无断言被放宽或删除。
