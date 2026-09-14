# AG-G2-APP-009-A01 — App Runtime 验证交接索引

- 日期：2026-09-14
- 本文件是 App Runtime（App Runner / Workflow → Runtime 闭环）验证工作的单一入口索引。

## 1. 当前位置

- 分支：`codex/agentscope-runtime`
- 当前 HEAD：`5d287e2b31be3e90644352c7022b3340745f8afa`（AG-G2-APP-008-A01 提交）
- 远端 HEAD：`origin/codex/agentscope-runtime` = `5d287e2b31be3e90644352c7022b3340745f8afa`（一致）
- 工作区：干净（本卡提交前）

## 2. 产物路径与提交（A01–A09）

| 卡 | 产物 | 提交 |
| --- | --- | --- |
| AG-G2-APP-001-A01（入口盘点） | `artifacts/acceptance/agentscope-runtime/AG-G2-APP-001-A01/20260914-a01/inventory.md` | `b8cf3e0` |
| AG-G2-APP-002-A01（生命周期映射） | `artifacts/acceptance/agentscope-runtime/AG-G2-APP-002-A01/20260914-a01/app-run-runtime-mapping.md` | `5f5571c` |
| AG-G2-APP-003-A01（首次 Runtime 闭环） | 实现提交 `12fdeb9`（`feat(app): bridge workflow runs to runtime`）；对应验收文档未入库（见 §6） | `12fdeb9` |
| AG-G2-APP-004-A01（独立闭环审查） | `AG-G2-APP-004-A01/20260914/app-runtime-closure-review.md` | `842f160` |
| AG-G2-APP-005-A01（结果可读性） | `AG-G2-APP-005-A01/20260914/app-runtime-user-observability.md` | `59f36c3` |
| AG-G2-APP-006-A01（失败路径与单调性） | `AG-G2-APP-006-A01/20260914/app-runtime-failure-monotonicity.md` | `f0a1de0` |
| AG-G2-APP-007-A01（手工 Runbook） | `AG-G2-APP-007-A01/20260914/app-runtime-manual-acceptance.md` | `30bea77` |
| AG-G2-APP-008-A01（发布门槛清单） | `AG-G2-APP-008-A01/20260914/app-runtime-release-gate.md` | `5d287e2` |
| AG-G2-APP-009-A01（本索引） | `AG-G2-APP-009-A01/20260914/handoff-index.md` | （本卡提交） |

（产物路径省略共同前缀 `artifacts/acceptance/agentscope-runtime/`。）

## 3. 核心事实速查

- **用户入口**：`POST /api/apps/{app_id}/run`（`app/gateway/routers/apps.py:485`，prefix `/api/apps` + `require_premium`；入口处 `require_app_visible` + 解析认证 org_id）。
- **开关**：`EVOFLOW_APP_WORKFLOW_RUNTIME`，服务端环境变量，默认关闭，真值 `1/true/yes/on`（`app/gateway/app_runtime_bridge.py::app_runtime_bridge_enabled`）。
- **幂等键**：`app-run:{app_run_id}`，Runtime repository 按 `(org_id, idempotency_key)` 复用，DB 唯一约束 `uq_qagent_runs_org_idempotency_key`。
- **旧状态源**：`evoflow_app_runs`（唯一用户可见业务状态源）；投影只经既有 `update_run_status` 写点。
- **状态映射**：completed→completed(progress=100,result_summary)；failed→failed(error)；timed_out→failed(error)（冻结语义）；cancelled→cancelled(error)；waiting_approval 永不投影。
- **回滚**：仅关闭环境变量并重启 Gateway；无数据库/schema/数据回滚。完整操作见 Runbook（A07）。

## 4. 最终精确测试与 Ruff 原始结果（2026-09-14，本卡执行）

```
uv run --frozen python -m pytest -q \
  app/tests/test_app_runtime_bridge.py \
  packages/harness/tests/test_workflow_run_dispatch.py \
  app/tests/test_g2_chat_runtime_result.py \
  packages/harness/tests/test_g2_chat_append_contract.py
→ 69 passed in 9.23s

uv run --frozen ruff check \
  app/gateway/app_runtime_bridge.py \
  app/gateway/routers/apps.py \
  packages/harness/evoflow/collab/app_runner.py \
  app/tests/test_app_runtime_bridge.py
→ All checks passed!
```

注：`test_workflow_run_dispatch.py` 实际位于 `packages/harness/tests/`（任务包文本写的 `app/tests/` 系笔误）；A06 未创建 `test_app_runtime_bridge_failures.py`（边界均可由 `test_app_runtime_bridge.py` 覆盖），故本回归不含该文件。

## 5. 已确认存量失败（parent/current 对比结论）

- `packages/harness/tests/test_app_revisions.py::test_save_app_writes_revision`、`::test_restore_app_from_revision`：在 parent `5f5571c`（`12fdeb9^`）与 current `12fdeb9` 两个隔离临时 worktree 独立运行均同形失败（`2 failed, 1 passed`，`ValueError: Revision not found: App_restore1@v1`）。结论：存量问题，与 Runtime bridge 无因果。对比过程见 A04 §7。

## 6. 未解决的产品 / 架构风险

1. **A03 验收文档未入库**：仓库只有实现提交 `12fdeb9`，无 AG-G2-APP-003 对应 artifacts 目录（交接缺口，建议补交归档）。
2. **自动 grant_approval 未记录 decided_by**（A04 §4 观察项）——生产前需补审计痕迹，另行派卡。
3. **取消语义边界**：旧 cancel 不停止 Runtime 侧已启动的 detached 执行（A07 步骤 7、A08 U5）。
4. **多实例 / 长时任务 / 真实并发 / 高可用 / 压测**：全部未验证（A08 U3/U6/U7），生产默认开启维持 NO-GO。
5. **G0 未冻结契约**：任何依赖 G0 契约冻结的判断均未在本次验证中做出。

## 7. 下一步边界声明

下一步若要扩大到生产灰度、多实例、审批语义、真实外部 workflow 或新持久化字段，**必须另行派卡**，不得在本卡之后自行开发。本任务包（AG-G2-APP-004-A01 至 AG-G2-APP-009-A01）到此收口。
