# AG-G2-APP-005-A01 — 原用户入口的结果可读性与状态一致性验收

- 日期：2026-09-14
- 前提：AG-G2-APP-004-A01 已完成（commit `842f160`），无阻断问题。
- 验证方式：在 `backend/app/tests/test_app_runtime_bridge.py` 追加 5 条最小直接测试（新增 section 8），使用**真实** `evoflow_app_runs` sqlite 库（`EVOFLOW_HOME` 隔离临时目录 + `reset_db_for_tests`），经既有查询函数 `app_repositories.load_run` 读回。未新增任何 API、SSE、表、字段或第二状态源；未修改生产逻辑。

## 逐项验证

### 1. Runtime completed 后，既有查询路径可读 completed / progress / completed_at / result_summary — ✅

- 测试：`test_completed_result_readable_via_existing_query_path`。
- 证据：桥接执行后 `app_repositories.load_run()` 返回 `status="completed"`、`progress=100`、`result_summary="季度汇总完成"`、`completed_at` 非空（`update_run_status` 对终态自动落 `completed_at`，见 `app_repositories.py:552-554`）。
- 结论：通过。

### 2. Runtime failed 后，既有查询路径可读 failed 和可用 error — ✅

- 测试：`test_failed_result_readable_with_error_via_existing_query_path`。
- 证据：`status="failed"`、`error="LLM provider unreachable"`（来自 Runtime error payload 的 message 字段）、`completed_at` 非空。
- 结论：通过。

### 3. Runtime cancelled 后不被轮询路径覆盖 — ✅

- 测试：`test_cancelled_survives_late_task_center_polling`。
- 证据：桥接写入 cancelled 后，用真实 `_sync_run_from_task`（Task Center 轮询同步点，`app_runner.py:511`）喂入 `task_status="completed", progress=100`，返回值与再读库结果均保持 `cancelled`（终态防回退守卫生效）。
- 结论：通过。

### 4. Runtime timed_out 映射为冻结的 failed 语义，error 非空 — ✅（附说明）

- 测试：`test_timed_out_maps_to_failed_with_nonempty_error`。
- 证据：`status="failed"`（无新枚举）、`error` 非空、`completed_at` 非空。
- 说明：冻结的 A03 投影对 timed_out/failed 只写 `error`，不写 `result_summary`（`app_runtime_bridge.py::project_runtime_result_to_app_run`）。用户可见的失败信息通过既有 `error` 字段完整可读，`result_summary` 保持空串是冻结语义的一部分，非缺口。按卡片第 8 条口径如实记录，不做猜测性生产改动。
- 结论：通过。

### 5. 开关关闭时，不产生 Runtime Run，旧状态与既有 workflow 行为一致 — ✅

- 测试：`test_switch_off_creates_no_runtime_run_and_leaves_run_untouched`。
- 证据：`EVOFLOW_APP_WORKFLOW_RUNTIME` 未设置时 `trigger_workflow_app_runtime_run` 返回 False，注入的 FakeRuntimeService `calls == []`（零读零写），App Run 保持原状；legacy 分发路径代码未被本提交改动（A04 已核 diff）。
- 结论：通过。

### 6. 开关开启时，evoflow_app_runs 仍是用户可见的唯一业务状态源 — ✅

- 证据：桥接全部终态写点为既有 `update_run_status`（`evoflow_app_runs` 表）；测试 1–4 均只通过 `load_run`（同表）读回终态。Runtime 侧 `qagent_runs` 仅作执行记录，不在任何既有 App Run 查询路径中出现。
- 结论：通过。

### 7. 不新增 API / SSE / 表 / 字段 / 第二状态源 — ✅

- 本卡改动仅测试文件与本文档；`git show 12fdeb9 --stat` 确认原实现亦无 schema 变更。

### 8. 结果摘要/错误是否总能从旧查询路径获得 — ✅（completed 有摘要，failed/cancelled/timed_out 有 error）

- completed：`result_summary` 由 Runtime result payload 派生（优先 summary/answer/text/output/result 键，兜底 JSON 序列化），测试证实可读。
- failed / cancelled / timed_out：`error` 字段非空可读；`result_summary` 为空是冻结映射语义（见第 4 条说明）。
- 无 Blocked 项。

## 改动文件

- `backend/app/tests/test_app_runtime_bridge.py`（追加 section 8：5 条测试 + 1 个 `real_run_db` fixture + 2 个辅助函数）

## 验收命令原始结果

```
uv run --frozen python -m pytest -q app/tests/test_app_runtime_bridge.py
→ 21 passed in 1.63s

uv run --frozen ruff check app/tests/test_app_runtime_bridge.py
→ All checks passed!

git diff --check / git diff --cached --check / git status --short
→ 干净
```

无阻断问题，可进入 AG-G2-APP-006-A01。
