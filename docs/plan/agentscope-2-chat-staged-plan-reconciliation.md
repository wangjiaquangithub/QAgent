# Chat worktree 已暂存 Plan / ADR 改动审查记录

- 文档角色：一次性审查与来源映射记录；**不是**第三套 Plan，不含任何新派工入口
- 审查执行卡：`PLAN-CHAT-STAGED-RECONCILIATION-001`
- 审查日期：2026 年 9 月 14 日
- 审查时集成分支：`codex/agentscope-runtime` @ `ea9ea363862c6c31242d62ea21623fed6b328f38`（`docs(plan): consolidate runtime execution cards`）
- 来源（只读）：`/Users/wangjiaquan/project/QAgent-g2-chat`（分支 `codex/g2-chat-live-run` @ `5881849`，审查对象为该 worktree 中**已暂存未提交**的改动）

## 0. 结论速览

**零归并修改。** 来源暂存批次的方向是"撤销 roadmap + execution-plan 双层拆分、回到单一 `foundation-plan.md`"，与负责人已冻结的权威结构（roadmap + execution-plan 两层；foundation-plan 不恢复）**正好相反**，且其中新增的 foundation-plan 与 `b09adf4` 旧版**逐字节一致**，不包含任何尚未被集成线覆盖的内容。因此本卡未修改 roadmap、execution-plan、ADR、验收矩阵，只写入本记录。

## 1. 来源暂存文件与逐文件结论

来源暂存清单（`git -C /Users/wangjiaquan/project/QAgent-g2-chat diff --cached --name-status`，相对其 HEAD `5881849`）：

| 来源文件 | 暂存类型 | 实质内容 | 三分类结论 | 处置 |
| --- | --- | --- | --- | --- |
| `docs/adr/003-agent-runtime-and-agentscope-2-adoption.md` | M | 仅 1 行：「关联计划」由 roadmap 链接改为 foundation-plan 链接 | 无效不归并 | 集成线 ADR 仍指向 roadmap，与冻结结构一致，不改 |
| `docs/plan/agentscope-2-acceptance-test-matrix.md` | M | 仅 1 行：「适用计划」由 roadmap 链接改为 foundation-plan 链接 | 无效不归并 | 集成线验收矩阵仍指向 roadmap，与冻结结构一致，不改 |
| `docs/plan/agentscope-2-enterprise-runtime-foundation-plan.md` | A | 新增 288 行 | 无效不归并 | 与 `b09adf4:docs/plan/...foundation-plan.md` **逐字节一致**（`diff -q` 通过），是对拆分前旧文档的原样恢复，不含新内容；且冻结决定明确不恢复该文件为权威入口 |
| `docs/plan/agentscope-2-enterprise-runtime-roadmap.md` | D | 删除 165 行 | 无效不归并 | 集成线版本 = 来源 HEAD 版本 + `ea9ea36` 的 3 行新增（`git diff --stat 5881849..HEAD` 实证），是严格更新版；执行删除只会丢失权威内容 |
| `docs/plan/agentscope-2-enterprise-runtime-execution-plan.md` | D | 删除 483 行 | 无效不归并 | 集成线版本 = 来源 HEAD 版本 + `ea9ea36` 的 111 行归并（同上实证），是严格更新版；执行删除只会丢失权威内容 |

## 2. 特别检查项结论

| 检查项 | 结论 |
| --- | --- |
| G0 四项待冻结决策是否被错误标记为已完成 | 否。来源暂存内容不含对 `G0-DEC-001`～`004` 状态的任何改写；集成线仍为 `Blocked` |
| G1 验收场景、取消 / claim / cursor / recovery 边界是否被弱化 | 否。集成线在 `ea9ea36` 已把来源 §5.1 七场景并入 `G1-GATE-001` 验收场景集；来源暂存的删除动作若被应用反而会移除全部 `AG-*` 卡（未应用） |
| G2 Chat / Automation 已完成范围是否被夸大为"全部业务迁移完成" | 否。来源暂存内容为拆分前的旧 Plan，不含 G2 完成度表述；集成线状态表（执行计划 §2.6）对 App Runner 明确标"未开始" |
| 手工验收、回滚、旧链路保留是否被删除或削弱 | 否。集成线保留 `agentscope-2-g2-chat-manual-acceptance.md`、`agentscope-2-g2-automation-manual-acceptance.md` 与 `G4-ROLLBACK-001` / `G4-CLEAN-*` 等回滚与旧链路下线卡；来源暂存的删除未应用 |
| ADR 是否有未进入权威文档但值得保留的架构边界 | 无。暂存差异只有 1 行链接指向替换，无任何架构边界内容 |

## 3. 待负责人决策

来源 Chat worktree 的整批暂存改动与冻结的权威结构方向相反（撤回双层拆分、恢复单一 foundation-plan）。本卡无权对该 worktree 做任何操作，仅提示需要负责人决定其处置方式：

1. 保留暂存不动（现状，不影响集成线）；
2. 由负责人自行撤销该批暂存（本卡及任何 Agent 均不得代为执行）；
3. 若负责人确有"回到单一 Plan"的意图，需先正式推翻当前冻结结论并重新定义权威结构——在此之前集成线不做任何配合改动。

## 4. 边界声明

本卡只新增本文件，未修改 `roadmap.md`、`execution-plan.md`、`acceptance-test-matrix.md`、`docs/adr/003`；未修改任何代码、测试、migration、依赖；未在来源 worktree 执行任何写操作（仅 `git status` / `git diff --cached` / `git show` / `git cat-file` 只读命令）；未使用 merge / cherry-pick / revert / switch / checkout / reset / rebase / restore / clean / stash；未删除任何分支或 worktree；未产生新的派工入口。
