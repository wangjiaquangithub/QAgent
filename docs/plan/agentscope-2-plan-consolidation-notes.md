# AgentScope 2.0 Runtime Plan 归并说明（G0–G4 拆卡成果 → 唯一权威 Plan）

- 文档角色：一次性归并记录，不承载实时进度
- 归并日期：2026 年 9 月 14 日
- 归并执行卡：`PLAN-CONSOLIDATION-001`
- 归并时集成分支：`codex/agentscope-runtime`
- 归并时集成分支提交：`bdafb35 merge(chat): fix idempotent message append`
- 归并时集成分支远端：`origin/codex/agentscope-runtime` = `bdafb35`

## 0. 唯一权威结构声明

自本次归并起，AgentScope 2.0 / QAgent Runtime 迁移计划**只有两层权威结构**：

1. `docs/plan/agentscope-2-enterprise-runtime-roadmap.md` —— 阶段、目标、依赖、放行门槛、架构原则与阶段顺序；
2. `docs/plan/agentscope-2-enterprise-runtime-execution-plan.md` —— **唯一** `AG-*` 可派工卡片与执行约束来源。

`docs/plan/agentscope-2-enterprise-runtime-foundation-plan.md` 曾作为拆卡分支的载体，**不作为长期权威 Plan、不恢复**（依负责人本轮决定）。该文件中仍然有效的内容已按下表逐项归并进上述两份文件；其余内容因与当前架构决策冲突或已被覆盖而丢弃，理由见第 2 节。

## 1. 归并来源

| 项 | 值 |
| --- | --- |
| 来源分支 | `origin/codex/g2-execution-workbreakdown` |
| 来源分支 HEAD | `0d73a1f0399ceb51c622c7d45a2cb581ec7b81c2` |
| 来源提交 | `23f7ff4 docs(plan): split G2 migration into executable slices` |
| 来源提交 | `0d73a1f docs(plan): break down G0 through G4 into task cards` |
| 来源文件 | `docs/plan/agentscope-2-enterprise-runtime-foundation-plan.md`（`0d73a1f` 版本，397 行） |
| 归并方式 | 逐卡映射 + 定向摘取执行约束；**未使用 git merge / cherry-pick**，未改变来源分支 |

来源分支相对集成分支的真实差异（`git diff HEAD...<source>`，自 merge-base `b09adf4` 起）只有上述 1 个文件，因此在第 2 节中逐项处理即可保证不丢失有效内容、也不重复出第三套计划。

## 2. 来源卡片 → 归并结论逐项映射

### 2.1 G0 / G1 卡片

| 来源卡片 | 内容摘要 | 归并结论 | 目标 / 理由 |
| --- | --- | --- | --- |
| G0.1 决策冻结 | 四项决策写成可执行枚举并签字 | 已覆盖 | 执行计划 `G0-DEC-001`～`G0-DEC-004`；来源"建议基线"作为待冻结参考，见第 4 节 |
| G0.2 Runtime 状态机契约 | Run / Approval / Attempt / Result / Recovery Point 状态与错误码 | 已覆盖 | `G0-DEC-001`、`G0-DEC-004` 与 `G1-CLM-001` 的状态机部分 |
| G0.3 执行 claim 与 epoch | acquire / 续租 / reclaim / 释放 / 拒写 | 已覆盖 | `G1-CLM-001`～`G1-CLM-007` |
| G0.4 Event / SSE 契约 | Event v1 字段、sequence、cursor、gap | 已覆盖 | `G0-DEC-003` + `G1-SSE-001` |
| G0.5 取消、恢复与新 attempt | 取消安全点与恢复规则 | 已覆盖 | `G0-DEC-001`、`G0-DEC-004` + `G1-REC-001`～`G1-REC-005` |
| G0.6 Adapter / Provider 错误边界 | AgentScope 异常 → 稳定 QAgent 契约 | 已覆盖 | `G1-ADP-001`、`G1-ADP-002`（当前计划把 Adapter 边界放在 G1，采当前权威结构） |
| **G0.7 PostgreSQL 真实并发验收** | 真实 PG 上的事务、多连接锁 | **已完成 / 已集成** | 集成提交 `fadddd0 merge(g0): integrate PostgreSQL concurrency acceptance`；对应 `G0-DB-003`，不重新派工 |
| G1.1 Approved Run 装配 | 从正式 Run 构造可执行请求 | 已覆盖 | `G1-EXE-001` |
| G1.2 AgentScope 真调用 | 真实 `Agent.reply(...)` | 已覆盖 | `G1-EXE-002`（仍需做） |
| G1.3 执行结果持久化 | Result / Asset / Recovery Point / 终态入库 | 已覆盖 | `G1-EXE-001` + `G1-CLM-006` + `G1-REC-003` |
| G1.4 Runtime Event 回放 | Event v1 经 API / SSE 按 cursor 回放 | 已覆盖 | `G1-SSE-002`（当前 `Implemented`）+ `G1-SSE-001` |
| G1.5 重启与重复触发恢复 | 重启后 reclaim 且不重复调用 Provider | 已覆盖 | `G1-CLM-003` + `G1-REC-004` + `G1-REC-005` |
| G1.6 G1 验收包 | 成功 / 失败 / 取消 / 重启 / 重复触发 / SSE / 越权 / 回滚可复现 | 已覆盖 | `G1-GATE-001`；来源 §5.1 七场景已并入其验收场景集（见执行计划归并章节） |

### 2.2 G2 卡片（本来源的主要新增内容）

| 来源卡片 | 内容摘要 | 归并结论 | 目标 / 证据 |
| --- | --- | --- | --- |
| G2.0 前置与总体规则 | 消费 G1 内核、不静默 fallback、旧链 G4 前保留、单卡 ≤6 文件 | 已归并为执行约束 | 执行计划归并章节"G2 波次执行约束" |
| G2.1-A 聊天 / Live Run 首次接入 | 原聊天入口 → Runtime Run → 终态回写 | **已完成 / 已集成** | `0679886 merge(chat): integrate Live Run runtime bridge`；另有 `bdafb35`（消息幂等修复）。证据文档：`agentscope-2-g2-chat-entry-map.md`、`agentscope-2-g2-chat-manual-acceptance.md` |
| **G2.1-B App Runner 首次接入** | 原 App 启动入口 → Runtime Run → 终态回写原 App Run | **仍待做 → 新卡** | 集成分支无 App Runner 运行时文件、无对应 merge 提交；见第 3 节 `AG-G2-APP-*` |
| G2.1-C 无人值守 / Task Center 首次接入 | 原任务入口 → Runtime Run → 终态回写 | **已完成 / 已集成** | `433a9df merge(automation): integrate Task Center runtime opt-in`。证据文档：`agentscope-2-g2-automation-completion-checklist.md`、`agentscope-2-g2-automation-entry-map.md`、`agentscope-2-g2-automation-manual-acceptance.md` |
| G2.2 旧业务状态投影 | Runtime 状态 → 旧记录状态字段 | 部分已完成 / 已集成 | 聊天：`chat_runtime_result.py`（`78ddcf6`）；自动化：`task_runtime_projection.py` 等；App 随 G2.1-B 待做 |
| G2.3 事件 / SSE 与流式兼容 | Event v1 → 原流式读取接口 | 部分已完成 / 已集成 | 聊天：`chat_runtime_stream_bridge.py`（`7dfffff`）；自动化：`task_runtime_event_bridge.py`、`task_runtime_cursor.py`；App 随 G2.1-B 待做 |
| G2.4 业务语义补齐 | 取消 / 重试 / 审批 / 资产 / 入口特有语义，每语义一张卡 | 部分已完成 / 已集成，余项不新造卡 | 聊天与自动化侧已有 `chat_runtime_*`、`task_runtime_cancel/retry/recovery/asset` 等实现；**未迁移余项以既有文档为准**：`agentscope-2-g2-automation-completion-checklist.md` §2/§4、`agentscope-2-g2-chat-manual-acceptance.md` §7/§8 |
| G2.5 用户可测业务包 | 负责人可在原页面 / 原 API 验收 | **已完成（聊天与自动化）** | 已存在 `agentscope-2-g2-chat-manual-acceptance.md`、`agentscope-2-g2-automation-manual-acceptance.md`，满足来源定义的验收包要件（成功 / 失败路径、环境变量、预期结果、回退、红线、记录表）；**不重新派工**。App 待做 |
| G2 业务域顺序与依赖矩阵 | 7 个业务域的迁移范围与并行规则 | 已覆盖 | 路线图 §5 与执行计划 §5（`G2-AGENT`～`G2-KNOW` 母任务）；来源"按入口派生首卡、同一入口串行、不同入口并行"的规则并入归并章节 |
| G2 任务卡强制模板 | 入口 / 回写 / 允许修改 / 禁止 / 暂不做 / 验证 / 完成标准 | 已归并 | 执行计划归并章节"G2 任务卡强制模板" |

### 2.3 G3 / G4 卡片

| 来源卡片 | 归并结论 | 目标 / 理由 |
| --- | --- | --- |
| G3.1 可观测性基线 | 已覆盖 | `G3-OBS-001`、`G3-OBS-002` |
| G3.2 备份、PITR 与恢复演练 | 已覆盖 | `G3-DB-001`（前置 `G0-DB-005`） |
| G3.3 容量与长稳 | 已覆盖 | `G3-LOAD-001`、`G3-LOAD-002` |
| G3.4 故障演练 | 已覆盖 | `G3-CHAOS-001`、`G3-OPS-001` |
| G3.5 多实例与 HA | 已覆盖 | `G3-HA-001` |
| G3.6 SQLite 历史数据迁移 | 阶段归属变更 | 当前权威结构将其列为 `G4-MIG-001`、`G4-MIG-002`（G3 只做加固，历史迁移与对账随切流同批）；采当前结构，不重复建卡 |
| G3.7 LangGraph 清理门 | 已覆盖 | `G4-CLEAN-001`、`G4-CLEAN-003`（只读清理扫描在 G4 收口） |
| G4.1 切流清单与冻结窗口 | 已覆盖 | `G4-CANARY-001` |
| G4.2 Shadow / 对账观察 | 已覆盖 | `G4-CANARY-002` |
| G4.3 首批租户灰度 | 已覆盖 | `G4-CANARY-001` + `G4-CANARY-002` 的批次验收 |
| G4.4 分批扩大 | 已覆盖 | `G4-CANARY-002`（每批独立观察与签字） |
| G4.5 回滚演练 | 已覆盖 | `G4-ROLLBACK-001` |
| G4.6 旧链路写入下线 | 已覆盖 | `G4-CLEAN-002` |
| G4.7 旧运行链路删除 | 已覆盖 | `G4-CLEAN-001` + `G4-CLEAN-003` + `G4-GATE-001` |

### 2.4 章级内容

| 来源章节 | 归并结论 | 目标 / 理由 |
| --- | --- | --- |
| §1 当前阶段 | 不采用 | 属时点快照；实时状态统一由执行计划维护 |
| §2 目标 | 已覆盖 | 路线图 §1.1 |
| §3 范围与能力归属（3.1 随迁门槛 / 3.2 可后置 / 3.3 不在范围） | 已覆盖 | 路线图 §1.2、§3、§4 各阶段放行门槛 |
| §4 实施路线与门槛 | 已覆盖 | 路线图 §4 + 执行计划 §3～§7 母任务 |
| §5.1 G1 最小验收集（7 场景） | **已归并为执行约束** | 执行计划归并章节"G1 验收场景集"，作为 `G1-GATE-001` 的验收场景 |
| §5.2 G2～G4 统一放行原则 | 已覆盖 | 执行计划 §8 统一放行检查（等价内容，不重复收录） |
| §6 四项待冻结决策 | 已覆盖 + 建议基线保留为参考 | `G0-DEC-001`～`G0-DEC-004`；建议基线见第 4 节 |
| §7 并行协作与责任边界 | 已覆盖 | 路线图 §6 责任边界 |
| §8 当前可执行工作清单 | 部分已完成、部分已转卡 | G0.7 已集成；CHAT / AUTO 的 G2.5 已存在；APP 项转为第 3 节新卡；G1.6 = `G1-GATE-001` |
| §8 所有卡统一回报格式 | **已归并为执行约束** | 执行计划归并章节"统一回报格式" |

### 2.5 未采用内容（明确废弃）

| 来源内容 | 废弃理由 |
| --- | --- |
| `docs/plan/agentscope-2-enterprise-runtime-foundation-plan.md` 作为长期入口 | 与"路线图 + 执行计划"两层结构重复；负责人本轮明确不再作为权威入口，不恢复 |
| 来源中以"业务域"为单位的巨型迁移任务（如"迁移聊天域"） | 与 G2 波次化拆卡规则冲突：G2 工作单位是**任务卡（一个入口 → 一次 Runtime 执行 → 一次终态回写）**，避免把大母任务伪装成 `AG-*` |
| 来源 §3.2 中把 SQLite 历史迁移、HA、多地域、全量 Chaos、全量压测列为"可在核心功能迁完后集中完成"的宽泛表述 | 与当前边界冲突风险高（易被误读为可跳过）；当前结构已把它们分别固定到 `G4-MIG-001/002`、`G3-HA-001`、`G3-CHAOS-001`、`G3-LOAD-002`。**HA / 多地域 / 全量 Chaos / 全量压测不作为当前业务迁移卡的内容** |
| 来源 §1 的时点状态描述（"尚未完成真实纵向执行链"等） | 已被集成分支事实取代，保留会造成误导 |

## 3. 本次新派工的 `AG-*` 卡片（已写入执行计划）

来源中唯一**仍待做且当前计划未覆盖**的工作是 App Runner 首次接入（来源 `G2.1-B`）。为避免在缺少入口证据时臆造文件清单，按当前计划的粒度门槛拆成三张卡：先盘点、再映射、最后接入。

| 新卡 ID | 母任务 | 波次 | 时间盒 |
| --- | --- | --- | --- |
| `AG-G2-APP-001-A01` | `G2-APP-001` | G2.1 盘点 | 60m |
| `AG-G2-APP-002-A01` | `G2-APP-002` | G2.1 映射 | 75m |
| `AG-G2-APP-003-A01` | `G2-APP-003` | G2.1 首次闭环 | 90m |

其余来源卡片一律**不新造卡**，理由见第 2 节（已覆盖 / 已完成 / 明确废弃）。

## 4. 待负责人冻结的决策

1. **四项 G0 契约（`G0-DEC-001`～`G0-DEC-004`）仍在 `Blocked`。** 本卡未取得任何负责人签署记录，故不改变其状态。来源 §6 给出的建议基线可作为待冻结输入（供决策参考，不构成冻结）：
   - Cancel 之后 Approval 的状态：建议取消只改变允许取消的 Run / attempt，Approval 是否终止需单独定义，不允许 Worker 隐式推断；
   - G1 是否必须持久化 execution claim：建议必须持久化；若选择不做，必须把单 Worker、不可 HA 写成部署限制并作为 G4 前阻断项；
   - Event 最小字段与续读规则：建议至少包含 `event_id`、`run_id`、`attempt_id`、`sequence`、`type`、`occurred_at`、状态 / 错误摘要、结果 / 恢复点引用；cursor 表示最后已确认 sequence，续读返回 `sequence > cursor`，缺口必须显式报错或补偿；
   - Recovery Point 最小持久化字段：建议至少包含 `recovery_point_id`、`run_id`、`attempt_id`、创建时间、可恢复状态、输入快照引用、已提交结果 / 副作用引用、Provider 请求引用、恢复策略、版本 / 校验值。
2. **`G0-DB-003` 是否可直接标 `Done`。** `fadddd0` 已把 PostgreSQL 真实并发验收合入集成分支，但本卡只读到提交标题，未核对其验收记录是否覆盖原 `AG-G0-DB-003-A01`～`A06` 六张卡的全部验收标准。本卡只把母任务标为 `Implemented` 并在其证据列登记该提交，**未标 `Done`**；请负责人对照 `fadddd0` 的验收记录确认后再定状态。确认前 `A01`～`A06` 不得重复派工。
3. **App Runner 入口的允许修改文件清单。** 集成分支当前没有任何 App Runner 运行时代码，`AG-G2-APP-001-A01` 的产物（`inventory.md`）才是唯一可信入口证据；`AG-G2-APP-003-A01` 的 ≤6 文件清单必须由该盘点产出登记后才能派工。
4. **G2 波次与现有 `G2-<域>-00x` 母任务的编号对齐。** 归并后同一入口同时存在于"域母任务（`G2-CHAT-00x`）"与"波次卡命名（`AG-G2-<域>-<波次>-A0x`）"两套编号下。本卡按来源波次命名新增 App 卡，未改写既有母任务编号；若负责人希望统一编号，需另行决定命名规则。

## 5. 边界声明

本卡只修改 `docs/plan/agentscope-2-enterprise-runtime-roadmap.md`、`docs/plan/agentscope-2-enterprise-runtime-execution-plan.md` 与本文件。未修改任何代码、测试、数据库、migration、Gateway、LangGraph、Chat、Automation、前端、依赖或 `docs/adr/`；未恢复 `foundation-plan.md`；未执行任何 `git merge` / `cherry-pick` / `revert`；未改动任何分支或 worktree 的结构。
