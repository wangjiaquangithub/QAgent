# AG-G2-APP-006-A01 — 失败路径、重试与终态单调性回归验收

- 日期：2026-09-14
- 前提：AG-G2-APP-005-A01 已完成（commit `59f36c3`），无阻断问题。
- 方式：仅增强 `backend/app/tests/test_app_runtime_bridge.py`（新增 section 9 + FakeRuntimeService 扩展 fail_on 到 grant_approval / get_result），未改生产逻辑。未新增 `test_app_runtime_bridge_failures.py`（真实调用边界均可由本文件覆盖，无需第二个测试文件）。

## 逐项覆盖

| # | 要求 | 测试 / 证据 | 结论 |
| --- | --- | --- | --- |
| 1 | create_run 失败 | `test_runtime_call_failure_lands_on_failed_terminal_never_legacy[create_run]`：detached 包装层将异常落到既有 failed 终态写路径，error 含 "Runtime bridge execution failed"，run 不卡死、不回落 legacy | ✅ |
| 2 | start_run 失败 | 同上 `[start_run]` | ✅ |
| 3 | grant_approval 失败 | 同上 `[grant_approval]`（FakeRuntimeService 新增 fail_on=grant_approval 分支） | ✅ |
| 4 | get_result 失败 | 同上 `[get_result]` | ✅ |
| 5 | completed 重复投影 | `test_repeated_terminal_projection_is_idempotent_and_monotonic[completed]`：第二次 `project_runtime_result_to_app_run` 返回 completed 且 writes 计数不变（无重复写、无回退） | ✅ |
| 6 | failed 重复投影不变回 executing/planned | 同上 `[failed]`，另用真实 `_sync_run_from_task(task_status="executing")` 验证晚到轮询保持 failed | ✅ |
| 7 | cancelled 重复投影不变回 executing/planned | 同上 `[cancelled]` | ✅ |
| 8 | timed_out → failed 映射稳定 | 同上 `[timed_out]`（重复投影仍为 failed）；另有 A03 起的 `test_timed_out_maps_to_existing_failed_enum`、A05 的真实库测试 | ✅ |
| 9 | 旧 Task Center 轮询晚到不覆盖 Runtime 终态 | A05 `test_cancelled_survives_late_task_center_polling`（真实 sqlite 库）+ 既有 `test_sync_run_from_task_does_not_regress_terminal`（completed/failed）+ 本卡重复投影测试中对 executing 晚到轮询的断言 | ✅ |
| 10 | 同 org 重复 app_run_id 使用相同幂等键 | 纯函数 `test_idempotency_key_derived_from_app_run_id` + `test_same_org_retry_reuses_runtime_run_in_real_repository`：真实 `RuntimeRepository`（内存 sqlite）两次 create_run 同 key 返回同一 run_id | ✅ |
| 11 | 跨 org 相同 app_run_id 文本按 org_id 隔离 | `test_cross_org_same_app_run_id_is_isolated_in_real_repository`：真实 repository，org-a 重试复用、org-b 得到不同 run；与既有 `test_qagent_runtime.py::test_idempotency_keys_are_scoped_by_organization` 互证（该测试用同样的内存 sqlite fixture） | ✅ |
| 12 | 开关关闭时 Runtime 调用均不发生 | 既有 `test_bridge_disabled_by_default`、`test_trigger_without_opt_in_reports_not_applicable`、A05 `test_switch_off_creates_no_runtime_run_and_leaves_run_untouched`（FakeRuntimeService.calls == []） | ✅ |

## 生产 bug 观察

未发现。所有失败边界都落在既有 `mark_app_run_failed_terminal` 终态写路径；无静默 legacy 回落；无卡死态。

一点如实记录（非 bug，行为确认）：失败终态的 error 文本由包装层统一生成（`"Runtime bridge execution failed: {exc}"`），不区分失败发生在哪个调用边界。若后续需要按边界分类错误码，需另行派卡改生产代码，本任务包不做。

## 改动文件

- `backend/app/tests/test_app_runtime_bridge.py`（section 9：4 个失败参数化用例 + 4 个终态重复投影参数化用例 + 2 个真实 repository 隔离用例；FakeRuntimeService 扩展 fail_on）

## 验收命令原始结果

```
uv run --frozen python -m pytest -q \
  app/tests/test_app_runtime_bridge.py \
  packages/harness/tests/test_workflow_run_dispatch.py
→ 34 passed in 5.27s
（test_app_runtime_bridge_failures.py 未创建，按卡规则从命令中省略）

uv run --frozen ruff check app/tests/test_app_runtime_bridge.py
→ All checks passed!

git diff --check / git diff --cached --check / git status --short
→ 干净
```

无阻断问题，可进入 AG-G2-APP-007-A01。
