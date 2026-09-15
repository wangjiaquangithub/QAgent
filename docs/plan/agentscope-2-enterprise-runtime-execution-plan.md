# AgentScope 2.0 Runtime 执行计划

- 文档角色：唯一实时执行进度来源；**唯一** `AG-*` 可派工卡片来源
- 更新日期：2026 年 9 月 15 日
- 适用路线图：[AgentScope 2.0 企业 Runtime 路线图](./agentscope-2-enterprise-runtime-roadmap.md)
- 关联 ADR：[ADR-003：QAgent Agent Runtime 与正式运行底座决策](../adr/003-agent-runtime-and-agentscope-2-adoption.md)
- 归并记录：[Plan 归并说明（G0–G4 拆卡成果 → 唯一权威结构）](./agentscope-2-plan-consolidation-notes.md)
- 当前分支：`codex/agentscope-runtime`
- 当前基线提交：`3c7aa5d4c0c1c7ed263d7cf285a03441f7da8a4a feat(chat): route live entry through runtime`

> 本计划按可直接派工的任务维护。状态以代码、测试和验收证据为准，不以旧计划中的“已完成”描述为准。本次只重写计划和引用，不修改 Runtime 代码、数据库 schema 或公共 API。
>
> 2026-09-14 起，本文件与路线图构成 Runtime 迁移的**唯一**权威计划结构：路线图只描述阶段、目标、依赖、放行门槛与顺序；本文件是唯一的 `AG-*` 卡片来源。`docs/plan/agentscope-2-enterprise-runtime-foundation-plan.md` 不再作为长期权威入口，其仍有效的内容已按 [归并记录](./agentscope-2-plan-consolidation-notes.md) 逐项并入。

## 1. 使用规则

### 1.1 状态定义

| 状态 | 判定规则 |
| --- | --- |
| **Done** | 代码已实现，自动化测试通过，必要的集成 / 生产验收证据存在，且没有未解决的 P0 / P1 正确性问题。 |
| **Implemented** | 代码已存在并有局部测试，但缺少真实环境、并发、故障或生产验收证据。 |
| **In Progress** | 正在开发，有明确 Owner、依赖和下一步动作。 |
| **Blocked** | 被决策、环境、外部服务或前置任务阻塞；必须写明解除条件。 |
| **Not Started** | 尚未开始，只有任务定义和依赖。 |
| **Deferred** | 已明确推迟到后续阶段，不属于当前阶段的阻塞项。 |

### 1.2 任务约束

- 每项任务默认控制在 1～3 个工作日；超过 3 天必须拆分或说明不可拆分原因。
- Owner 使用角色而不是虚构姓名：`Runtime`、`DB`、`SSE`、`Domain Migration`、`Observability`、`Release / Integration`。
- 依赖必须引用任务 ID 或决策 ID；没有依赖写 `—`。
- 修改范围必须指向具体目录、模块、接口、脚本或证据目录。
- 验收标准必须能由另一位工程师独立判断通过 / 不通过。
- 测试与证据必须包含命令、测试名、日志、迁移记录、对账记录或演练编号；“人工确认”不能单独作为证据。
- 新任务完成后在本文件更新状态、完成日期和证据路径；路线图不重复记录实时进度。

### 1.3 Agent 执行协议（防止长时间循环）

本计划的 `1～3d` 是**人类交付母任务**的工作量，不是 Coding Agent 的单次执行时长。Coding Agent **禁止直接领取母任务**；只能领取下方的 `AG-*` 子任务卡片。没有 `AG-*` 卡片的任务，先由负责人拆卡，不得让 Agent 自行拆解并扩大范围。

| 规则 | 硬限制 | 触发后的动作 |
| --- | --- | --- |
| 单次范围 | 一次只执行一个 `AG-*` 子任务；完成后必须停下，不能自动领取下一个任务 | 输出结果并等待新的明确指令 |
| 时间盒 | 默认 30～90 分钟，绝对上限 120 分钟 | 超过 90 分钟仍未满足验收标准，先暂停并拆卡；超过 120 分钟必须停止并标记 `Blocked` 或 `Not Started` |
| 修改范围 | 只允许修改子任务卡片列出的文件；默认不超过 3 个核心代码文件 + 1 个测试 / 证据文件 | 发现需要扩大范围时停止，提出新的子任务，不得自行扩散 |
| 首个动作 | 先读取卡片指定的入口文件和直接调用方，再执行卡片指定的唯一首条命令 | 不得先做全库扫描、全量测试或重写架构 |
| 测试重试 | 同一命令最多执行 2 次；同类错误连续出现 2 次必须停止 | 记录完整命令、退出码、最后 100 行输出和判断 |
| 无进展 | 连续 20 分钟没有缩小文件范围、错误范围或待决策范围 | 立即停止，报告当前假设和需要的人类决策 |
| 依赖阻塞 | 缺数据库、凭据、Provider、决策或前置任务时不得猜测替代 | 标记 `Blocked`，写明阻塞原因、解除条件和已完成检查 |
| 代码边界 | 不为通过测试修改无关代码；不修改未授权的 Runtime、schema、公共 API 或用户已有改动 | 回滚本次无关修改并停下报告 |
| 完成判定 | 只有卡片验收命令通过、产物已保存、修改文件清单完整时才算完成 | 否则只能是 `In Progress`、`Blocked` 或 `Not Started` |
| 交接格式 | 必须输出：修改文件、执行命令、测试结果、证据路径、剩余风险、下一步 | 缺一项不得关闭卡片 |

**禁止循环的明确停止条件：**

1. 同一个失败命令第 2 次失败后停止，不得换参数盲试超过卡片范围。
2. 需要修改第三个以上核心模块时停止，先拆出依赖卡片。
3. 需要改变状态机、数据库字段、公共 API 或契约时停止，转为 `DEC` 决策卡，不能边猜边改。
4. 发现测试基线与卡片预期不一致时停止，不得通过删除、跳过或放宽测试来“完成”。
5. 卡片完成后停止，即使发现了相邻问题，也只能在交接中列出，不得顺手处理。

### 1.4 Agent 派工模板（强制）

执行计划中的一行表格**不是完整派工指令**。负责人每次只能把一张 `AG-*` 卡片复制成一条独立派工消息；不得把整个 G0/G1 阶段、一个母任务或多张卡片一次性发给 Agent。

派工消息必须使用以下模板，并把尖括号内容替换成卡片中的实际值：

```text
你只执行一个子任务：<AG-* ID>。

目标：<子任务列的原文，一句话，不增加范围>
首个动作：先读取 <精确文件 / 目录>，然后只执行这一条命令：
  <唯一首条命令>
允许修改：<精确文件列表或证据目录>
禁止修改：<schema / 公共 API / 其他业务域 / 用户已有改动 / 未授权文件>
时间盒：<卡片时间盒>，绝对上限 120 分钟。
验收：<唯一验收命令或客观标准>
停止条件：<卡片停止条件>；同一命令第二次失败、20 分钟无进展或需要扩大修改范围时立即停止。

完成后只输出：
1. 修改文件（没有则写“无”）；
2. 执行过的完整命令、退出码和结果；
3. 证据路径；
4. 剩余风险 / 阻塞原因；
5. 下一步建议（不得自动执行）。

完成或阻塞后立即停止，不领取下一张卡片，不继续修复相邻问题。
```

以下派工方式禁止使用：

- “把 G1 claim 做完”“完善 Runtime”“排查所有测试失败”等开放式目标；
- 同时粘贴多个 `AG-*` ID，或允许 Agent 自己选择下一项任务；
- 没有精确首个文件、首条命令、允许修改路径和停止条件的口头任务；
- 让 Agent 在缺少 PostgreSQL、Provider、凭据或状态契约时反复尝试替代方案；
- 把“实现或验证”“完善”“处理全部问题”作为一个卡片目标。此类表述必须先拆成盘点、实现、测试或证据卡。

### 1.5 粒度门槛与拆卡规则

一张 `AG-*` 卡片只有同时满足以下条件才算“可直接派工”：

1. 只有一个主要动作类型：`盘点`、`决策草案`、`实现`、`测试` 或 `证据汇总`，不能混合多个主要动作；
2. 只有一个主要产物；例如测试卡产出一个测试文件或一份测试报告，不能同时要求实现、迁移和部署；
3. 只有一个唯一验收命令，且命令失败时有明确停止路径；
4. 允许修改范围可列出具体文件；出现第三个以上核心模块、schema、公共 API 或跨业务域依赖时必须拆卡；
5. 预计超过 90 分钟的卡片必须说明为什么不能再拆；绝对不能超过 120 分钟；
6. 卡片不能同时包含两个独立业务断言。例如“事务回滚 + org 隔离 + 幂等”应拆成三张验证卡，除非它们由同一个不可拆分的测试命令一次性证明。

如果执行中发现卡片不满足上述门槛，Agent 不得自行扩大范围；应在交接中标记 `Blocked: card-too-broad`，列出建议的新卡片边界，由负责人重新派工。

### 1.6 Agent 子任务卡片字段

每张 `AG-*` 卡片必须写清以下字段；缺字段的卡片不得派工：

| 字段 | 要求 |
| --- | --- |
| Agent 子任务 ID | 全局唯一，例如 `AG-G1-CLM-007-A03` |
| 母任务 | 只能引用本执行计划中的一个任务 ID |
| 首个动作 | 一个明确文件入口和一条首条命令 |
| 允许修改文件 | 精确到文件或目录；证据卡片应明确“只读代码、只写 artifacts” |
| 禁止修改范围 | 例如 Runtime schema、公共 API、其他业务域、用户已有改动 |
| 时间盒 | 30～120 分钟；超过 120 分钟必须拆卡 |
| 唯一验收命令 | 一条可重复命令；需要多步时拆成多张卡 |
| 完成产物 | 代码 / 测试 / mapping / 日志 / `verdict.md` 中的一种主要产物 |
| 停止条件 | 明确什么情况下立即停下并标记阻塞 |
| 交接输出 | 修改文件、命令、结果、证据、风险和下一步 |

### 1.7 母任务与子任务的关系

- 本文原有的 G0～G4 任务是**交付母任务**，用于里程碑、依赖和放行管理；它们不是 Agent 的直接执行单元。
- `AG-*` 是实际派给 Coding Agent 的最小执行单元。母任务只有在其全部子任务、证据和放行条件满足后才能更新为 `Done` 或 `Implemented`。
- G2～G4 的母任务在进入对应阶段前必须按本节模板实例化为 `AG-*` 卡片；未实例化前只能保持 `Not Started`，不得伪装成可直接执行。
- 决策类任务只能由 Agent 生成决策草案、测试向量和影响清单；Agent 不得替负责人拍板。

### 1.8 证据目录

默认保存到：

```text
artifacts/acceptance/<release-or-build>/<task-id>/<attempt>/
├── input.json
├── identifiers.json
├── timeline.jsonl
├── events.jsonl
├── db-checks/
├── logs/
└── verdict.md
```

正式环境不得写入真实密钥、长期 token、文件原件或未脱敏企业数据。证据至少能关联 `org_id`、`run_id`、`event_id`、`attempt_id`、`recovery_point_id` 或迁移批次 ID。

## 2. 当前状态摘要

### 2.1 已有基线证据

当前已执行并通过：

```bash
cd /Users/wangjiaquan/project/QAgent/backend
PYTHONPATH=. .venv/bin/pytest -q \
  app/tests/test_agentscope_compat.py \
  app/tests/test_qagent_runtime.py \
  app/tests/test_qagent_runtime_agentscope.py \
  app/tests/test_qagent_runtime_router.py
```

结果：`27 passed in 4.81s`（2026-09-13）。该结果只证明离线 / SQLite 测试基线通过，不等于 G1 生产放行。

### 2.2 已实现但仍缺生产证据

- AgentScope `2.0.8` 锁定和兼容性 probe。
- PostgreSQL Runtime migration、`qagent_runs`、`qagent_run_events`、`qagent_approvals`、`qagent_recovery_points`、`qagent_run_assets`。
- Bearer 鉴权、organization 隔离、organization 级幂等。
- Approval → Run → Error / Event 的事务链。
- AgentScope Adapter、Provider factory、server-side executor、HTTP API、SSE、cancel / resume / result。
- 启动时未完成 Run 扫描和恢复入口。

以上项目按本计划标为 `Done` 或 `Implemented`，具体以任务表为准；真实 PostgreSQL、真实 Provider、claim / lease / fencing、重启 / 故障注入和 SSE 断线续读仍需补证据。

### 2.3 当前明确阻塞

- `G0-DEC-001`：cancel 后 Approval 最终状态。
- `G0-DEC-002`：G1 是否强制持久化 execution claim。
- `G0-DEC-003`：Event v1 字段、sequence、cursor、断线续读和 gap 语义。
- `G0-DEC-004`：Recovery Point 与 attempt 语义。

### 2.4 当前 Agent 就绪度与第一批子任务卡片

**结论：原来的 101 个 G0～G4 条目对人工派工够细，但对自主 Coding Agent 不够细。**它们仍然是 1～3 天的母任务，不能直接交给 Agent。下面先把当前最容易造成“执行 10 小时仍在循环”的 G0 / G1 任务拆成 30～120 分钟的 `AG-*` 卡片。

- 本节卡片仍是 `Not Started`，因为本次只重写计划，不执行 Runtime 代码变更。
- Agent 只能领取本节已定义的 `AG-*` 卡片；未列出 `AG-*` 卡片的母任务一律不可直接派工。
- `AG-*` 卡片完成后必须停下；是否领取下一张卡片由人类负责人明确决定。

#### G0 当前第一批卡片

| ID | 母任务 | 阶段 | 子任务 | 状态 | Owner | 依赖 | 时间盒 | 允许修改范围 | 唯一验收命令 / 标准 | 完成产物与停止条件 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| AG-G0-DB-003-A01 | G0-DB-003 | G0 | 盘点真实 PostgreSQL 测试入口、环境变量、fixture 和清理策略 | Not Started | DB | G0-DB-001、G0-DB-002 | 45m | 只读 `backend/scripts/postgres_verify.py`、相关测试；只写 `artifacts/.../G0-DB-003-A01/` | `rg -n -e 'postgres' -e 'DATABASE' -e 'migration' -e 'fixture' backend/scripts/postgres_verify.py backend/app/tests`；入口、变量和缺口各有一条记录 | `inventory.md`；找不到可用环境或命令不完整时立即 `Blocked`，不得自行改部署体系 |
| AG-G0-DB-003-A02 | G0-DB-003 | G0 | 启动一次 disposable PostgreSQL 并记录版本、连接和清理结果 | Not Started | DB | AG-G0-DB-003-A01 | 60m | 只写测试环境配置和证据；禁止改 Runtime schema | `cd backend && .venv/bin/python scripts/postgres_verify.py`；命令成功且数据库版本、revision、清理结果可追溯 | `input.json`、`logs/`、`verdict.md`；缺 Docker / 数据库 / 凭据第二次检查仍失败就停止 |
| AG-G0-DB-003-A03 | G0-DB-003 | G0 | 在真实 PostgreSQL 上执行现有 migration 并核对核心表和索引 | Not Started | DB | AG-G0-DB-003-A02 | 75m | 只读 migration / model；只写 `db-checks/` 和证据 | `cd backend && .venv/bin/python scripts/postgres_verify.py`；foundation、Runtime revision、核心表和唯一约束均有输出 | `db-checks/schema.txt`；发现 schema 需要设计变更时转 `G0-DEC`，不得直接改 migration |
| AG-G0-DB-003-A04 | G0-DB-003 | G0 | 验证真实 PostgreSQL 上的事务回滚、org 隔离和幂等 | Not Started | DB | AG-G0-DB-003-A03 | 90m | 只允许修改指定 PG fixture / 测试文件；禁止改业务实现 | 指定 PG 集成测试命令通过；失败时保存 SQL、连接和事务日志 | `events.jsonl`、`db-checks/`、测试输出；同一类失败两次立即停止 |
| AG-G0-DB-003-A05 | G0-DB-003 | G0 | 验证两个数据库连接的最小并发行为 | Not Started | DB | AG-G0-DB-003-A03 | 90m | 只写一个并发测试文件和证据；禁止引入 claim 设计 | 双连接测试能明确报告获得锁 / 提交 / 回滚顺序；若无法证明则不得标 Done | 并发 timeline；需要 execution claim 才能完成时转 `G0-DEC-002` |
| AG-G0-DB-003-A06 | G0-DB-003 | G0 | 汇总真实 PostgreSQL 集成证据和缺口 | Not Started | DB | AG-G0-DB-003-A02～A05 | 45m | 只写 `artifacts/.../G0-DB-003/` | `test -s artifacts/acceptance/<release>/G0-DB-003/<attempt>/verdict.md`；verdict 明确通过项、失败项和下一步 | `verdict.md`；任何未解释失败都保持 `In Progress` / `Blocked` |
| AG-G0-DB-004-A01 | G0-DB-004 | G0 | 列出当前 migration revision、升级顺序和可回滚边界 | Not Started | DB | G0-DB-003 | 45m | 只读 `backend/migrations/versions/`、Alembic 配置；只写证据 | `rg -n -e 'revision' -e 'down_revision' -e 'def upgrade' -e 'def downgrade' backend/migrations/versions`；每个 revision 有顺序和风险 | `migration-map.md`；发现 downgrade 不可逆时停止并记录风险 |
| AG-G0-DB-004-A02 | G0-DB-004 | G0 | 在空库执行一次完整 upgrade 并保存 revision 证据 | Not Started | DB | AG-G0-DB-004-A01 | 60m | 只写 disposable DB 和证据 | `cd backend && .venv/bin/python scripts/postgres_verify.py`；upgrade 从空库到 head 可重复 | upgrade 日志；环境不可复现时停止 |
| AG-G0-DB-004-A03 | G0-DB-004 | G0 | 对单个失败点执行失败、重试和清理验证 | Not Started | DB | AG-G0-DB-004-A02 | 90m | 只写故障注入 fixture / migration 验收脚本；禁止改生产 migration 逻辑 | 失败后数据库状态可解释，重试不产生半残对象或重复约束 | failure log、retry log、db snapshot；需要改 schema 才能继续时转决策卡 |
| AG-G0-DB-004-A04 | G0-DB-004 | G0 | 形成 migration verdict，不把“命令跑完”当作回滚通过 | Not Started | DB | AG-G0-DB-004-A03 | 45m | 只写证据 | verdict 明确 upgrade、失败、重试、downgrade 各自结果 | `verdict.md`；任何一项没有证据则不得标 Done |
| AG-G0-DB-005-A01 | G0-DB-005 | G0 | 确认备份、恢复、脱敏和 PITR 所需输入 | Not Started | DB | G0-DB-003 | 60m | 只读运维配置；只写 `backup-inputs.md` | 输入清单包含工具版本、权限、备份位置、恢复目标和不可使用的数据 | `backup-inputs.md`；缺权限或输入未确认时 `Blocked` |
| AG-G0-DB-005-A02 | G0-DB-005 | G0 | 生成最小脱敏 Runtime fixture 和恢复前计数 | Not Started | DB | AG-G0-DB-005-A01 | 75m | 只写 fixture、SQL 和证据；禁止使用真实企业数据 | fixture 可重复生成；Run、Event、org、关系计数有快照 | `input.json`、`db-checks/before.txt`；发现脱敏不足立即删除并停止 |
| AG-G0-DB-005-A03 | G0-DB-005 | G0 | 恢复到隔离库并核对 schema、Run、Event 和 org 边界 | Not Started | DB | AG-G0-DB-005-A02 | 90m | 只写隔离库和证据 | restore 后计数、约束、org 隔离与 before 快照一致；差异逐项解释 | `db-checks/after.txt`、restore log；恢复失败第二次仍失败则停止 |
| AG-G0-DB-005-A04 | G0-DB-005 | G0 | 记录恢复耗时、RPO / RTO 输入和未覆盖项 | Not Started | DB | AG-G0-DB-005-A03 | 45m | 只写 runbook / evidence | `verdict.md` 明确实测时间、目标时间和未做的 PITR，不把输入确认冒充 PITR 完成 | `verdict.md`；无法实测时保持 `Implemented` / `Not Started` |
| AG-G0-DEC-001-A01 | G0-DEC-001 | G0 | 起草 cancel 与 Approval 竞态状态矩阵 | Not Started | Release / Integration | — | 60m | 只写 ADR 草案 / 测试向量；禁止改实现 | 矩阵覆盖 queued、running、cancel、approve、terminal 和重复请求 | `cancel-state-matrix.md`；Agent 不得做最终决策 |
| AG-G0-DEC-002-A01 | G0-DEC-002 | G0 | 起草 claim、lease、fencing 的最小契约和单 Worker 限制对照 | Not Started | Release / Integration | — | 60m | 只写决策草案；禁止改 schema / worker | 明确 G1 必选项、若不做的部署限制和放行影响 | `claim-decision-draft.md`；缺生产部署信息时停止提问 |
| AG-G0-DEC-003-A01 | G0-DEC-003 | G0 | 起草 Event v1 字段、sequence、cursor、重复和 gap golden vectors | Not Started | SSE + Runtime | — | 90m | 只写协议草案和 vectors；禁止改 SSE 实现 | 至少覆盖成功、失败、重复、断线、gap、终态倒退 | `event-v1-vectors.jsonl`；协议争议转人工评审 |
| AG-G0-DEC-004-A01 | G0-DEC-004 | G0 | 起草 Recovery Point、attempt 和已提交副作用语义 | Not Started | Runtime + Release / Integration | — | 90m | 只写契约草案和 vectors；禁止改 recovery 实现 | 至少覆盖成功、失败、重启、迟到结果和重复副作用 | `recovery-vectors.jsonl`；无法定义边界时保持 `Blocked` |

#### G1 当前第一批卡片

| ID | 母任务 | 阶段 | 子任务 | 状态 | Owner | 依赖 | 时间盒 | 允许修改范围 | 唯一验收命令 / 标准 | 完成产物与停止条件 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| AG-G1-CLM-001-A01 | G1-CLM-001 | G1 | 将已批准的 claim 状态图翻译为字段、状态转换和测试向量 | Not Started | Runtime | G0-DEC-002 | 75m | 只改指定 claim 契约 / 测试文件；禁止自行新增字段 | 状态图、字段、转换和负例一一对应 | `claim-contract.md`、vectors；决策未冻结时停止 |
| AG-G1-CLM-002-A01 | G1-CLM-002 | G1 | 实现 claim 获取的最小事务路径；已有实现满足契约时只补缺口与测试 | Not Started | Runtime | AG-G1-CLM-001-A01、G0-DB-003 | 90m | 只改 claim repository 和一个测试文件 | 单 worker 获取成功；无 claim / 已占用 / 终态均有负例 | 指定 claim 单测；需要改非 claim 模块时停止 |
| AG-G1-CLM-002-A02 | G1-CLM-002 | G1 | 实现 claim 释放路径并覆盖正常完成、失败、cancel | Not Started | Runtime | AG-G1-CLM-002-A01 | 75m | 只改 claim release path 和一个测试文件 | 三种终态都不会留下可误用的有效 claim | release 测试输出；发现终态语义未决转 `G0-DEC-001` |
| AG-G1-CLM-003-A01 | G1-CLM-003 | G1 | 验证 lease 未过期时不能被第二 worker 接管 | Not Started | Runtime | AG-G1-CLM-002-A01 | 75m | 只改并发测试 / fixture | 两 worker timeline 明确只有 owner 有效 | 并发测试证据；环境不支持双 worker 时停止 |
| AG-G1-CLM-003-A02 | G1-CLM-003 | G1 | 验证 lease 过期后只能被新 worker 接管一次 | Not Started | Runtime | AG-G1-CLM-003-A01 | 90m | 只改 recovery / concurrency 测试 | 过期、接管、重复接管三种结果可观察 | kill / reclaim timeline；不能证明唯一接管则不关闭 |
| AG-G1-CLM-004-A01 | G1-CLM-004 | G1 | 为迟到 worker 的 Event / Result 写入增加最小 fencing 负例 | Not Started | Runtime | G1-CLM-003 | 90m | 只改 commit guard 和测试；禁止改 Event v1 契约 | 旧 epoch 写入被拒绝或标记 stale，不能覆盖新 attempt / 终态 | stale-write 测试；字段契约未冻结时停止 |
| AG-G1-CLM-005-A01 | G1-CLM-005 | G1 | 验证 Provider 调用前的 claim preflight | Not Started | Runtime | G1-CLM-002、G1-CLM-004 | 75m | 只改 executor preflight 和 fake Provider 测试 | 无效 claim、过期 claim、fencing 不匹配时 Provider call count 为 0 | fake Provider 计数；需改变 Provider 抽象时拆新卡 |
| AG-G1-CLM-006-A01 | G1-CLM-006 | G1 | 验证结果提交时的 claim、attempt 和终态保护 | Not Started | Runtime | G1-CLM-004 | 90m | 只改 result commit guard 和测试 | stale result 不覆盖新 attempt、cancelled 或 completed Run | result guard 测试；状态机未决时停止 |
| AG-G1-CLM-007-A01 | G1-CLM-007 | G1 | 编写双 worker claim 竞争测试 | Not Started | Runtime + Integration | AG-G1-CLM-002-A01、G0-DB-003 | 75m | 只写并发测试和证据 | 两 worker 中只有一个实际执行；另一个有明确拒绝原因 | concurrency timeline；DB 不可用时停止 |
| AG-G1-CLM-007-A02 | G1-CLM-007 | G1 | 编写 worker 崩溃后 lease 接管测试 | Not Started | Runtime + Integration | AG-G1-CLM-003-A02 | 90m | 只写故障测试和证据 | kill 后未过期不接管，过期后只接管一次 | kill / restart timeline；无法控制进程时 `Blocked` |
| AG-G1-CLM-007-A03 | G1-CLM-007 | G1 | 编写 fencing token 迟到写入测试 | Not Started | Runtime + Integration | AG-G1-CLM-004-A01 | 75m | 只写 stale write 测试和证据 | 旧 worker 的 Event、Result、终态写入均不能覆盖新 owner | stale-write verdict；协议未冻结时停止 |
| AG-G1-CLM-007-A04 | G1-CLM-007 | G1 | 编写 Provider 不可逆副作用计数测试 | Not Started | Runtime + Integration | AG-G1-CLM-005-A01 | 75m | 只写 controllable Provider fixture 和测试 | 重试、恢复、重复投递的外部调用次数符合契约 | provider-call-count.json；副作用语义未冻结时停止 |
| AG-G1-CLM-007-A05 | G1-CLM-007 | G1 | 汇总 claim 并发 / 崩溃 / 迟到结果验收 | Not Started | Runtime + Integration | AG-G1-CLM-007-A01～A04 | 45m | 只写 evidence / verdict | 五类场景全部有结果、失败项和风险 Owner | `verdict.md`；有未解释失败不得标 Done |
| AG-G1-REC-001-A01 | G1-REC-001 | G1 | 将 cancel 合法检查点转换为 queued / running / provider-call / terminal 测试矩阵 | Not Started | Runtime | G0-DEC-001、G0-DEC-004 | 75m | 只写测试向量和契约；禁止猜测终态 | 每个检查点有允许动作、拒绝原因和事件要求 | `cancel-checkpoints.md`；契约未签署时停止 |
| AG-G1-REC-002-A01 | G1-REC-002 | G1 | 实现 queued cancel 的授权、幂等和事件测试 | Not Started | Runtime | AG-G1-REC-001-A01 | 75m | 只改 cancel endpoint / test | 重复 cancel 不产生第二个终态或第二个事件 | cancel unit test；越权行为发现后停止扩大范围 |
| AG-G1-REC-002-A02 | G1-REC-002 | G1 | 实现 provider-call 与 cancel 竞态的最小测试 | Not Started | Runtime | AG-G1-REC-001-A01、AG-G1-CLM-005-A01 | 90m | 只改 cancel race fixture / test | 竞态结果符合契约，已完成结果不被覆盖 | race timeline；需要新状态时转决策卡 |
| AG-G1-REC-003-A01 | G1-REC-003 | G1 | 写入 Recovery Point 的最小持久化和脱敏测试 | Not Started | Runtime | G0-DEC-004、G0-DB-003 | 90m | 只改 recovery point repository 和测试 | 每个 record 关联 Run / attempt / version，敏感输入不落库 | recovery DB check；契约未冻结时停止 |
| AG-G1-REC-004-A01 | G1-REC-004 | G1 | 验证 resume 创建新 attempt 且跳过已完成副作用 | Not Started | Runtime | AG-G1-REC-003-A01、AG-G1-CLM-003-A02 | 90m | 只改 resume path / test | 新旧 attempt 可区分；Provider 副作用计数不重复 | resume timeline；无法判定副作用状态时停止 |
| AG-G1-REC-005-A01 | G1-REC-005 | G1 | 编写非优雅重启后恢复和迟到结果保护测试 | Not Started | Runtime + Integration | AG-G1-REC-004-A01、AG-G1-CLM-004-A01 | 120m | 只写 restart integration test / evidence | 新 attempt 可完成，旧 worker 不能覆盖终态 | restart timeline、verdict；超过 120 分钟拆为两张卡 |
| AG-G1-SSE-001-A01 | G1-SSE-001 | G1 | 将 Event v1 草案转换为 schema 和字段级 golden vectors | Not Started | SSE + Runtime | G0-DEC-003 | 90m | 只写 schema、vectors、测试；禁止改业务执行 | 所有必填字段、未知字段和终态事件均有样例 | schema / vector diff；协议未签署时停止 |
| AG-G1-SSE-002-A01 | G1-SSE-002 | G1 | 验证 `after_sequence` 只返回后续事件并保持顺序 | Not Started | SSE | G0-DEC-003、G0-DB-003 | 60m | 只写查询测试和证据 | sequence 边界、空结果和重复请求结果确定 | SSE query test；发现 gap 语义缺失转 `G0-DEC-003` |
| AG-G1-SSE-003-A01 | G1-SSE-003 | G1 | 编写断线后按 cursor 续读测试 | Not Started | SSE | AG-G1-SSE-002-A01 | 90m | 只写 SSE reconnect test / fixture | 断线前后不丢事件；重放范围可由 cursor 解释 | reconnect timeline；客户端语义未决时停止 |
| AG-G1-SSE-003-A02 | G1-SSE-003 | G1 | 编写重复事件去重和客户端幂等测试 | Not Started | SSE | AG-G1-SSE-003-A01 | 75m | 只写 SSE client / protocol test | 重复 event_id 不造成第二次业务提交；顺序仍可解释 | dedupe test；需要改业务写路径时拆卡 |
| AG-G1-SSE-004-A01 | G1-SSE-004 | G1 | 编写 sequence gap 检测和明确恢复动作测试 | Not Started | SSE | G0-DEC-003、AG-G1-SSE-002-A01 | 90m | 只写 gap detector / test | gap 被识别，并返回重放、重新订阅或明确错误之一 | gap verdict；恢复动作未决时转人工决策 |
| AG-G1-SSE-005-A01 | G1-SSE-005 | G1 | 编写多订阅者顺序和终态不可倒退测试 | Not Started | SSE + Integration | AG-G1-SSE-003-A01、AG-G1-SSE-004-A01 | 90m | 只写 SSE integration test / evidence | 同一 Run 的多订阅者顺序一致，终态之后不能出现有效运行状态 | ordering timeline；需要改变 Event schema 时停止 |

#### G2～G4 的拆卡规则

G2～G4 当前仍然是阶段母任务，尚未开始，不提前伪装成 Agent 已就绪。每个业务域 / 生产加固 / 灰度任务在进入执行前，必须实例化下面的最小卡片链，并把 `<PARENT>` 替换成唯一母任务 ID：

| 卡片 | 目的 | 时间盒 | 主要产物 | 不允许做的事 |
| --- | --- | --- | --- | --- |
| `AG-<PARENT>-A01` | 只读盘点一个入口、一个表组或一个写路径 | 45～60m | `inventory.md` | 不跨业务域搜索，不修改代码 |
| `AG-<PARENT>-A02` | 固化一个状态 / ID / Event / org 映射 | 60～90m | `mapping.md` 或 vectors | 不自行改变公共契约 |
| `AG-<PARENT>-A03` | 接入或切换一个最小路径 | 90～120m | 一个代码变更 + 一个测试 | 不同时改恢复、灰度和旧链路清理 |
| `AG-<PARENT>-A04` | 验证一个成功或失败场景 | 45～90m | 一个测试结果 | 不把全量测试作为默认验收 |
| `AG-<PARENT>-A05` | 验证一个恢复、回滚或对账场景 | 60～120m | timeline / SQL / 对账结果 | 不把未完成的异常留给“以后再看” |
| `AG-<PARENT>-A06` | 汇总证据并给出母任务状态建议 | 30～45m | `verdict.md` | 不自动关闭母任务，不自动领取下一张卡 |

G2 业务域至少需要为“盘点、映射、接入、正式写入 / 恢复、测试 / 回滚 / 对账、灰度观察”各实例化一组卡片；G3 / G4 的性能、故障、备份、灰度、回滚和旧链路下线也必须逐项实例化。**在卡片实例化前，G2～G4 任务仍保持 `Not Started`。**

## 2.5 G2 波次化拆卡与执行约束（归并自拆卡分支）

本节内容归并自 `origin/codex/g2-execution-workbreakdown`（`0d73a1f`）的有效拆卡成果，逐项映射见 [归并记录](./agentscope-2-plan-consolidation-notes.md)。本节只新增约束与 App 入口的新卡，不改写既有 `G2-<域>-00x` 母任务编号。

### 2.5.1 波次顺序（同一入口串行，不同入口可并行）

```text
G2.1 首次闭环  →  G2.2 状态投影  →  G2.3 事件 / SSE 兼容  →  G2.4 业务语义（每语义一张卡）  →  G2.5 用户可测业务包
```

- **G2 的工作单位是任务卡，不是业务域。** 每张 G2.1 卡只交付：一个真实旧入口 → 一次 Runtime 执行 → 一次旧业务记录终态回写 → 可从原 API 或原页面验证的结果。
- 同一入口必须依次经过 G2.1 → G2.2 → G2.3 → G2.4 → G2.5；不同入口的 G2.1 可以并行。
- 一个入口的“可给负责人测”状态只要求完成该入口的 G2.1 与 G2.5；G2.2～G2.4 的实际完成范围必须在验收包中如实标记，不得假称全域迁移完成。

### 2.5.2 写域与过程约束

- 不同入口使用独立 worktree / 分支，写域不重叠；任何两个 Agent 不得同时修改同一旧业务目录、同一旧状态表或同一个前端文件。
- `backend/app/qagent_runtime/` 与 `backend/migrations/versions/` 始终由 Runtime 负责人独占；业务卡发现 Runtime API 缺口时只提交最小接口需求与复现，不得直接修改内核。
- 编辑前列出允许修改文件，原则上不超过 6 个；超出即拆新卡。
- 只读当前入口及最多两个直接必要的调用点；首个实际 diff 的时间盒为 15 分钟，超时只能报告一个已核实、不可绕过的具体阻塞，不得继续泛读。
- 旧 LangGraph 链路在 G4 前保留以支持回滚，但**已接入 Runtime 的入口不得在 Runtime 失败时静默 fallback 到 LangGraph**；失败必须让原入口得到明确、可读的失败结果。
- 本阶段明确不吸收：SQLite 历史数据迁移、HA / 多地域、全量 Chaos、全量压测、设备 lease / fencing 与 scheduler HA（分别属于 `G4-MIG-001/002`、`G3-HA-001`、`G3-CHAOS-001`、`G3-LOAD-002`）。

### 2.5.3 G2.1 单卡完成标准

1. 真实旧入口创建或关联一个可追溯的 Runtime Run；
2. 该 Run 经 Runtime 实际执行，不用 mock 结果、不静默回退到 LangGraph；
3. `completed` 或 `failed` 终态及最终结果 / 错误回写到对应旧业务记录；
4. 原 API 或原页面可读取该结果，且跨 org / 无权限访问不泄漏；
5. 至少一个聚焦该入口的自动化测试，以及不超过 8 步的手工测试步骤；
6. 交付报告给出入口、业务记录 ID、`run_id`、验证命令、验证结果和未覆盖项。

### 2.5.4 新增 `AG-*` 卡片（App 入口）

Owner 随母任务；三张卡按“先盘点、再映射、最后接入”串行，`AG-G2-APP-003-A01` 的允许修改文件清单必须由 `AG-G2-APP-001-A01` 的产物登记后才能派工。

| ID | 母任务 | 阶段 | 目标（单一动作） | 依赖 | 允许修改范围 | 禁止范围 | 唯一验收命令 / 标准 | 停止条件 | 时间盒 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| AG-G2-APP-001-A01 | G2-APP-001 | G2 | 只读盘点 App 启动入口、旧业务记录与写路径，并给出可复用的 Runtime 公开调用点，产出 `inventory.md` 与允许修改文件清单 | — | 只读 App 启动 / Run 相关模块与既有 Adapter 调用点；只写 `artifacts/acceptance/<release>/AG-G2-APP-001-A01/<attempt>/inventory.md` | 不改任何代码；不改 `backend/app/qagent_runtime/`、`backend/migrations/versions/`、Gateway 主干与 router registry；不删 / 替换 LangGraph；不改前端 | 先读 `agentscope-2-g2-chat-entry-map.md` 与 `agentscope-2-g2-automation-entry-map.md` 作对照，再只读定位入口；`inventory.md` 必须同时给出：①真实入口（前端或 HTTP）②对应旧业务记录与终态字段 ③可复用 Runtime 公开调用点 ④允许修改文件清单（≤6 个）。缺任一项不得标 `Done` | 入口不唯一、需要改 Runtime 内核、或需要新增 schema / 第二状态源时立即停止并转决策卡 | 60m | Not Started |
| AG-G2-APP-002-A01 | G2-APP-002 | G2 | 固化 App Run 与 Runtime Run / Event / 结果 / 终态回写字段的映射 | AG-G2-APP-001-A01 | 只读代码；只写 `artifacts/acceptance/<release>/AG-G2-APP-002-A01/<attempt>/mapping.md` | 不改 Runtime 契约与 schema；不新增字段；不改公共 API；不改 Gateway 主干 | `mapping.md` 对每个 App Run 状态、终态字段、结果引用、错误摘要与 org 权限给出唯一对应，并显式列出无法一一对应的项（含原因） | 需要新增 schema、第二状态源或改变公共 API 时停止；不得自行拍板映射语义 | 75m | Not Started |
| AG-G2-APP-003-A01 | G2-APP-003 | G2 | 打通一个真实 App 启动入口 → 一次 Runtime 执行 → 终态回写原 App Run，并可从原 API 读取 | AG-G2-APP-001-A01、AG-G2-APP-002-A01 | 以 `AG-G2-APP-001-A01` 产出的 ≤6 个文件清单为准（清单未登记前不得派工） | 不改 `backend/app/qagent_runtime/`、`backend/migrations/versions/`；不改前端页面结构与导航；不改其他业务域；不删 / 替换 LangGraph；不得静默 fallback | 一个聚焦该入口的自动化测试通过，且 5～8 步手工验证在原页面 / 原 API 走通成功与失败路径；报告给出入口、业务记录 ID、`run_id`、验证命令、验证结果、未覆盖项 | 需要改 Runtime 内核 / schema、需要第二状态源、或同类失败第 2 次出现时停止 | 90m | Not Started |

### 2.5.5 G2 任务卡强制模板

```text
【任务名称】G2.<波次>-<业务入口>-<单一目标>
【目标】只打通 <一个现有真实入口> 到 Runtime 的 <一个可读取结果>。
【入口与回写】旧入口/API：…；旧业务记录：…；Runtime Run：…；终态回写字段：…。
【允许修改】开始编辑前列出不超过 6 个文件/目录；超出即拆新卡。
【禁止修改】backend/app/qagent_runtime/；backend/migrations/versions/；前端页面结构或导航；其他业务域；删除/替换 LangGraph。
【暂不做】SSE、断线恢复、所有子类型、设备、多实例、完整审批、HA、压测、全量迁移（除非本卡名称明确包含其中一项）。
【自动化验证】一个聚焦测试 + 必要的既有回归；不跑无关全仓检查。
【手工验证】原页面/API 的 5～8 步成功/失败验证；列出预期结果与 run_id/业务记录证据。
【完成标准】真实入口关联 Runtime Run；Runtime 实际执行；终态回写；原页面/API 可读；测试与手工步骤均可复现。
【过程约束】只读入口和最多两个直接必要调用点；15 分钟内出现首个 diff；没有 diff 时只报告一个具体、不可绕过的阻塞。
```

### 2.5.6 G1 验收场景集（用于 `G1-GATE-001`）

`G1-GATE-001` 的验收场景至少覆盖下表的全部场景（归并自拆卡分支 §5.1）：

| 场景 | 必须证明 |
| --- | --- |
| 成功执行 | Approved Run 经真实 `AgentScope Agent.reply(...)` 产生结果；Run 状态、Result、Event、Recovery Point 均可追溯 |
| Provider 失败 | 失败分类、错误事件与 Run 终态一致；可按合法策略重试或转人工处置；不重复副作用 |
| 取消 | 取消按已冻结的状态机与安全检查点处理；不把已完成结果覆盖为旧状态 |
| 服务重启 | 重启前后的 Run / Event / Recovery Point 可从 PostgreSQL 恢复；不创建第二个执行者 |
| 重复触发 / 回执 | 幂等键与 claim 使重复请求、迟到事件、重复回执收敛，不重复执行、不覆盖结果 |
| SSE 断线 | 客户端可用 cursor / sequence 续读；事件顺序可验证；缺口可发现而非静默跳过 |
| 授权 | 跨 org 或无权限访问被拒绝；服务端不信任客户端传入的 org / owner 上下文 |

### 2.5.7 统一回报格式（所有 `AG-*` 卡片）

```text
【卡片】Gx.y-名称 / AG-*
【工作树 / 分支】…
【旧入口 / 原页面或 API】…
【Runtime Run / 旧业务记录证据】run_id=…；business_id=…
【改动文件】…（不超过该卡范围）
【自动化】命令 + 原始结果
【手工验证】5～8 步 + 成功 / 失败实际结果
【未覆盖 / 阻塞】只列本卡明确未做项；若阻塞，给出最小复现和所需接口
【Git】commit / push 状态；不得夹带其他 worktree 修改
```

## 2.6 已集成入口与剩余业务迁移原子卡（截至 2026-09-15）

本节是当前剩余业务迁移的派工边界。已完成事项仅用于防止重复派工。A01 / A02 只覆盖真实证据已经定位、且不需要修改生产代码、测试、schema 或 migration 才能完成的盘点 / 映射工作；A03～A05 只在 A01 / A02 已能写出真实旧文件与**已存在的** Runtime 调用点时实例化，且必须逐卡写明允许修改文件、唯一验收命令与停止条件。

### 2.6.1 业务迁移现状矩阵

| 业务域 | 当前状态 | 已核实证据 | 本轮结论 |
| --- | --- | --- | --- |
| Apps / Workflow | 真实新链首闭环已完成；TEST GO，PROD NO-GO | 提交 `12fdeb9`、`b8cf3e0`、`bec812e`、`f0a1de0`、`bffbf5c`、`e8a0d13`、`f290af9`；`artifacts/acceptance/agentscope-runtime/AG-G2-APP-009-A01/20260914/handoff-index.md` | 已完成、不可重派 A01～A05；生产放行仍受既有 G0/G3 证据约束，不在本轮补派业务实现卡 |
| Chat / Live Run | 真实新链首闭环已完成 | 提交 `3c7aa5d4c0c1c7ed263d7cf285a03441f7da8a4a`；`docs/plan/agentscope-2-g2-chat-entry-map.md`；`docs/plan/agentscope-2-g2-chat-manual-acceptance.md`；flag-on 旧 LangGraph checkpoint=0，flag-off 保持旧链 | 已完成、不可重派 A01～A05 |
| Automation / Task Center | 部分接入 / bridge，整域未完成；Run 建立已闭环，**终态回写与读取出口未接线** | 提交 `433a9df merge(automation): integrate Task Center runtime opt-in`；`docs/plan/agentscope-2-g2-automation-entry-map.md`、`docs/plan/agentscope-2-g2-automation-completion-checklist.md`、`docs/plan/agentscope-2-g2-automation-manual-acceptance.md`；生产接线点仅 3 处（见 A01 卡） | 新增 A01 盘点、A02 映射，并实例化 A03～A05（只补终态回写与既有读取出口，不重派 Run 建立） |
| Agent / 数字员工 | 尚未接入；真实入口、旧事实源、状态机已定位；**harness 内 Runtime 接点为 0** | `/api/proactive`（`backend/packages/harness/evoflow/proactive/router.py`）、`/api/platform` 的 `employees.*` 动作（`evoflow/admin/platform_actions.py`、`platform_handlers.py`、`admin/employees.py`）、后台心跳 `backend/app/gateway/background_startup.py:1030` `start_proactive_runner`；表 `evoflow_proactive_roles`/`_initiatives`/`_approvals`、`evoflow_agents`、`evoflow_agent_runtime` | 新增 A01（补真实入口）与 A02 映射；A03～A05 不实例化 |
| Goal / 协同 | 尚未接入；真实入口、旧任务 / 审批 / 协同事实源已定位；**harness 内 Runtime 接点为 0** | `/api/goal`（`backend/app/gateway/routers/goal.py:13`）、`/api/collab`（`backend/app/gateway/routers/collab.py:23`）、`backend/app/channels/services/goal_service.py`；表 `evoflow_goal_sessions`、`evoflow_collab_tasks`/`_subtasks`/`_peer_messages`/`_task_execution_history`、`evoflow_proactive_approvals`、`evoflow_tool_approvals` | 新增带 G0 依赖确认的 A01 与 A02 映射；A03～A05 不实例化；不得猜测 cancel、claim、Event 或 recovery 语义 |
| Workspace / 文件 / 资产 | 尚未接入；资产事实源已定位 | `evoflow_artifacts`、`evoflow_media_assets`、`evoflow_org_artifacts`；`backend/packages/harness/evoflow/persistence/schema.py:247-255, 807-823, 986-993` | 仅新增 A01；不扩展到存储 HA、灾备或历史迁移 |
| Knowledge / Memory / Tools / MCP / Channels | 尚无足够统一入口证据 | 目前只确认需从真实用户业务入口、旧事实源和外部副作用边界开始；未确认统一 Runtime 接点 | 仅新增 A01；A02～A05 不实例化 |

### 2.6.2 新增可执行 `AG-*` 卡片

卡片的“允许修改”是执行该卡时的写域，不授权修改本执行计划之外的生产文件。A01 / A02 均为只读盘点或映射卡，不做实现；证据目录只有在实际执行卡片时按卡片约定创建。

**剩余业务迁移第一批拆分范围（2026-09-15）**：Automation / Task Center、数字员工 / Agent、Goal / 协同。实例化判据与已核实锚点记录在非权威记录 [G2 第一批拆卡侦察与实例化记录](./agentscope-2-g2-batch1-reconnaissance-notes.md) 中；该记录不是派工来源，只解释卡片为何可实例化或必须停在上游。

- A01 / A02 一律实例化：只读证据卡，不依赖 G0 决策即可产出。
- A03～A05 只在 A01 / A02 已能写出**真实旧文件**且**已存在的 Runtime 调用点**时才实例化；本轮只有 Automation / Task Center 满足该条件，且只覆盖其**尚未接线**的终态回写与既有读取出口。
- 已完成的 Run 建立首闭环（`433a9df`）、Apps / Workflow 首闭环与稳定化、Chat / Live Run 首闭环均不重派。

#### AG-G2-AUTO-001-A01：Automation / Task Center 真实入口与旧状态机盘点

- **母任务 / 状态 / Owner / 时间盒**：`G2-AUTO-001` / `Not Started` / Domain Migration / 60m。
- **单一目标**：只读确认一个或多个真实入口、旧业务事实源、调用链、状态机和 Runtime bridge 的实际边界，产出入口清单与缺口清单。
- **真实入口**：`backend/app/gateway/routers/automation_scheduler.py`、`backend/app/gateway/routers/tasks.py`、`backend/app/gateway/routers/events.py`；同时核对 App Runner 绑定工作流自动化、prompt-only 默认 LangGraph、scheduler 定时触发和无人值守身份传播路径。
- **旧业务事实源 / 写路径**：`backend/app/gateway/events/task_events.py`、`backend/app/gateway/automation_runner.py`、`backend/app/gateway/task_queue_runner.py`、`backend/app/gateway/task_runtime_optin.py`、`backend/app/gateway/task_runtime_projection.py`；登记 Task Center 状态、事件与重启后回投影的现有写点，不新增状态源。
- **Runtime 调用点 / 边界**：只读记录现有 `task_runtime_*` bridge / projection 的调用关系。2026-09-15 已核实：生产代码中只有 3 处接线 —— `backend/app/gateway/unattended_task_pipeline.py:599 _maybe_run_via_runtime`（经 `task_runtime_optin.establish_runtime_run` 建立 Run）、`backend/app/gateway/task_queue_runner.py:119 decide_runtime_pickup`（已关联则跳过推进）、`backend/app/gateway/routers/tasks.py:1919 _cancel_linked_runtime_run_if_any`（经 `task_runtime_cancel.cancel_linked_runtime_run` 取消）。`task_runtime_projection`、`task_runtime_event_bridge`、`task_runtime_cursor`、`task_runtime_recovery`、`task_runtime_reconcile`、`task_runtime_degrade`、`task_runtime_asset`、`task_runtime_result`、`task_runtime_input_snapshot`、`task_runtime_failure` 均已实现并有单测，但**除测试与彼此引用外没有任何生产调用点**，即 Run 状态与终态当前不会回写到 Task Center 记录。该事实是本域 A03 的唯一依据；无人值守 Run 创建若出现默认 `org_id="local"` 只登记为缺口，不修正语义。
- **允许修改范围**：只写 `artifacts/acceptance/agentscope-runtime/AG-G2-AUTO-001-A01/<attempt>/inventory.md`；不超过 1 个证据文件。
- **禁止触碰**：所有 backend 生产代码、测试、schema、migration、配置、Runtime 内核、Gateway 主干重构、前端、Chat、App Runner、HA / 多实例 / 备份 / 灾备 / 压测 / 历史迁移。
- **验收命令 / 产物**：`rg -n "automation_scheduler|task_runtime_|TaskAuthorizedEvent|TaskExecutionStartedEvent|TaskExecutionFailedEvent|TaskCancelEvent|TaskCancelledEvent|TaskResumeEvent" backend/app/gateway`；命中点、真实入口、旧事实源、写域、bridge 边界和缺口全部写入 `inventory.md`。
- **停止条件**：入口无法唯一定位；需要修改 Runtime 内核、schema、migration 或第二状态源；发现与正在进行的 Chat 实现重叠；或需要处理 HA / 多实例 / 运维事项。

#### AG-G2-AUTO-002-A01：Automation / Task Center 对象与 Runtime 字段级映射

- **母任务 / 状态 / Owner / 时间盒**：`G2-AUTO-002` / `Not Started` / Domain Migration + Runtime / 75m。
- **单一目标**：在 `AG-G2-AUTO-001-A01` 产物基础上，建立 Task Center 对象、状态、事件、取消、终态与 Runtime Run / Event / Result / Cancel 的字段级对照；未知项必须标为 `Blocked`，不得补猜。
- **真实入口 / 旧事实源**：沿用 A01 已核实的 scheduler、task、event 入口与 `task_events.py`、`automation_runner.py`、`task_queue_runner.py` 写路径；不得扩展扫描范围。
- **Runtime 调用点 / 边界**：沿用已存在的 `task_runtime_optin.py`、`task_runtime_projection.py` 及 A01 登记的 bridge；只记录可从源码证明的调用点和字段。
- **映射范围**：至少覆盖 `TaskAuthorizedEvent`、`TaskExecutionStartedEvent`、`TaskExecutionFailedEvent`、`TaskCancelEvent`、`TaskCancelledEvent`、`TaskResumeEvent`，以及 Task Center 终态、错误 / 结果引用和幂等键；`cancel` 后 approval、claim 所有权、Event v1 / cursor / gap、Recovery Point / attempt 受 G0 决策影响的列标记阻塞。已冻结的 Runtime 状态 → `TaskStatus` 映射以 `docs/plan/agentscope-2-g2-automation-completion-checklist.md` §1.4 为准（`waiting_approval` 不变更任务状态、`timed_out` → `failed`、其余终态同名收敛），本卡只做对照登记，不重新定义、不新增 `TaskStatus` 枚举值。
- **允许修改范围**：只写 `artifacts/acceptance/agentscope-runtime/AG-G2-AUTO-002-A01/<attempt>/mapping.md`；不修改任何源代码或计划外文档。
- **禁止触碰**：不得新增字段、接口、schema、状态机或 fallback；不得实现 cancel / recovery / claim / cursor；不得触碰 HA、备份、灾备、压测、历史迁移或其他业务域。
- **验收命令 / 产物**：`rg -n "TaskAuthorizedEvent|TaskExecutionStartedEvent|TaskExecutionFailedEvent|TaskCancelEvent|TaskCancelledEvent|TaskResumeEvent|task_runtime_" backend/app/gateway`；`mapping.md` 逐字段列出证据文件 / 符号、确定值、未知值、G0 决策依赖和 A03 是否可创建。
- **停止条件**：A01 未完成；任一字段需要臆造；发现必须修改 Runtime / schema / migration 才能闭合；或 G0 语义未冻结导致只能选择实现方案。

#### AG-G2-AUTO-003-A01：最小真实入口 Runtime 首闭环（终态回写与既有读取出口）

- **母任务 / 状态 / Owner / 时间盒**：`G2-AUTO-003` / `Not Started` / Domain Migration + Runtime / 120m。
- **单一目标**：在**既有** unattended 收敛点，把**已关联** Runtime Run 的状态与终态投影回既有 Task Center 任务记录，使既有 API 能读到 Runtime 结果；不新建 Run 建立路径、不改读取出口。
- **不重派声明**：Run 建立与关联（`433a9df`，`AG-G2-AUTO-003-B01`）已完成且不可重派；本卡只补 `AG-G2-AUTO-001-A01` 已核实的「已实现但零生产调用点」缺口。
- **真实入口**：`backend/app/gateway/unattended_task_pipeline.py:599 _maybe_run_via_runtime`（既有收敛点，已接线）、`backend/app/gateway/task_queue_runner.py:71 task_queue_tick`（队列 tick）、`backend/app/gateway/routers/tasks.py:2674 GET /api/tasks/{task_id}/runtime` 与既有 `GET /api/tasks/{task_id}`、`GET /api/tasks/{task_id}/execution-history`。
- **旧业务事实源 / 写路径**：任务行本身（`get_project_storage()` / `_patch_task`）的 `status`、`unattended_stage`、`execution_history`；只通过既有写路径回写，不新增状态源、不新增表或字段。
- **可复用且已存在的 Runtime 调用点**：`task_runtime_optin.default_runtime_contract()`（返回 `RuntimeService`，公开方法 `get_run_status(run_id)`）；已实现待接线的 `task_runtime_projection.project_runtime_status`、`task_runtime_cursor.advance_runtime_cursor`、`task_runtime_reconcile.reconcile_linked_task_runtime`、`task_runtime_degrade.reconcile_task_runtime_safely`、`task_runtime_result.sanitize_result_summary`。
- **允许修改范围**：`backend/app/gateway/unattended_task_pipeline.py`、`backend/app/gateway/task_queue_runner.py`，以及**新增** 1 个测试文件 `backend/app/tests/test_task_runtime_terminal_writeback.py`；生产文件合计 2 个，未用满 6 个上限前不得再扩大。
- **禁止触碰**：`backend/app/qagent_runtime/`、`backend/migrations/versions/`、`backend/app/gateway/routers/tasks.py`（读取出口已存在）、Event v1 / sequence / cursor / gap 语义、cancel 与 approval 语义、`TaskStatus` 枚举、前端、LangGraph 主链、schema 与配置。
- **唯一验收命令 / 标准**：`cd backend && PYTHONPATH=. .venv/bin/pytest -q app/tests/test_task_runtime_terminal_writeback.py app/tests/test_task_runtime_api_reads.py`；要求 Runtime `completed` / `failed` / `cancelled` / `timed_out` 经既有 API 读到的任务状态与 `execution_history` 符合已冻结映射，终态不倒退，且未关联任务与开关关闭时行为逐字节不变。
- **完成产物**：1 个最小接线 diff + 1 个聚焦测试。
- **停止条件**：需要改 `qagent_runtime` / schema / migration；需要新增 `TaskStatus` 值或新表；需要 Event v1 流式帧、cursor 或 gap 语义（转 `G0-DEC-003`）；需要跨实例触发幂等（转 `G0-DEC-002`）；需要改动前端或 LangGraph 主链。

#### AG-G2-AUTO-004-A01：终态回写路径的幂等、失败与终态不倒退定向测试

- **母任务 / 状态 / Owner / 时间盒**：`G2-AUTO-004` / `Not Started` / Domain Migration / 90m。
- **单一目标**：只针对 `AG-G2-AUTO-003-A01` 接线的回写路径补定向负例，不新增功能。
- **必须覆盖**：同一 Run 重复 tick 与重复投影不产生第二条终态记录；乱序 / 迟到状态不覆盖已写入终态；`failed` 与 `timed_out` 的错误摘要经既有脱敏函数处理；`runtime_run_linkage` 不可解码时不得被当作「无 Run」而重复推进；开关关闭时不产生任何 Runtime 调用。
- **允许修改范围**：只**新增** `backend/app/tests/test_task_runtime_writeback_boundaries.py`；只读复用 `backend/app/tests/_runtime_flow_support.py`，不修改既有测试。
- **禁止触碰**：任何生产代码、`backend/app/qagent_runtime/`、schema、migration、Event v1 契约、cancel / approval 语义、其他业务域测试。
- **唯一验收命令 / 标准**：`cd backend && PYTHONPATH=. .venv/bin/pytest -q app/tests/test_task_runtime_writeback_boundaries.py`；全部通过，且任一失败都指向 `AG-G2-AUTO-003-A01` 的实现缺陷而不是放宽断言。
- **完成产物**：1 个测试文件 + 原始输出。
- **停止条件**：需要修改生产代码才能通过时立即停止并回报，不得为通过测试放宽或删除断言；`cancel` 与 approval 耦合子场景标记 `Blocked by G0-DEC-001`，本卡不得断言其终态语义。
- **依赖**：`AG-G2-AUTO-003-A01`。

#### AG-G2-AUTO-005-A01：真实 HTTP / 页面手工验收 Runbook

- **母任务 / 状态 / Owner / 时间盒**：`G2-AUTO-005` / `Not Started` / Domain Migration + Release / 90m。
- **单一目标**：用**既有** HTTP 入口完成一次成功、一次失败、一次取消的端到端手工验收，产出 Runbook 与证据；不做代码改动。
- **真实入口（全部为既有 API，不得新增）**：`POST /api/tasks`（`run_mode=unattended`）、`POST /api/tasks/{task_id}/start`、`POST /api/tasks/queue/tick`、`GET /api/tasks/{task_id}`、`GET /api/tasks/{task_id}/execution-history`、`GET /api/tasks/{task_id}/runtime`、`POST /api/tasks/{task_id}/cancel`。
- **开关**：`EVOFLOW_AUTOMATION_UNATTENDED_RUNTIME=1`；回退为取消该变量，不删除任何历史数据。
- **允许修改范围**：只写 `artifacts/acceptance/agentscope-runtime/AG-G2-AUTO-005-A01/<attempt>/runbook.md` 与 `verdict.md`，并附命令原始输出与 `run_id` / `task_id` 关联证据。
- **禁止触碰**：所有生产代码、测试、schema、migration、配置、前端、其他业务域。
- **唯一验收命令 / 标准**：`test -s artifacts/acceptance/agentscope-runtime/AG-G2-AUTO-005-A01/<attempt>/verdict.md`；Runbook 必须为 5～8 步、每步给出预期与实际结果，并显式列出未覆盖项（Event 流式续读、gap、跨实例幂等）。
- **完成产物**：`runbook.md`、`verdict.md`。
- **停止条件**：真实 Gateway / PostgreSQL / Provider 不可用第二次仍失败即 `Blocked`；不得用 mock 结果、截图代替原始命令输出。
- **依赖**：`AG-G2-AUTO-003-A01`。

#### AG-G2-AGENT-001-A01：数字员工 / Agent 真实入口与事实源盘点

- **母任务 / 状态 / Owner / 时间盒**：`G2-AGENT-001` / `Not Started` / Domain Migration / 60m。
- **单一目标**：只读定位数字员工 / Agent 的真实用户入口、旧事实源、状态 / 会话写路径、旧执行链与可证明的 Runtime 接入边界。
- **真实入口（2026-09-15 已核实，需在产物中登记为证据）**：`backend/packages/harness/evoflow/proactive/router.py`（`APIRouter(prefix="/api/proactive")`，含 `POST /{agent_code}/dispatch`、`/{agent_code}/heartbeat`、`/{agent_code}/pause|resume|stop`、`/{agent_code}/work-board`、`/{agent_code}/performance`），经 `backend/app/gateway/router_registry.py:126-128` 挂载；`/api/platform` 动作入口 `backend/packages/harness/evoflow/admin/platform_actions.py:349-368` 的 `employees.list|get|hire|update|pause|resume|stop|archive|worklog|trail`，处理函数在 `backend/packages/harness/evoflow/admin/platform_handlers.py:747-842`，实现在 `backend/packages/harness/evoflow/admin/employees.py`（`hire:441`、`update_role:572`、`pause_role:706`、`stop_role:725`、`resume_role:754`、`archive_role:789`、`worklog:961`、`round_trail:1142`、`dispatch:1245`、`wake:1402`）；后台心跳循环 `backend/app/gateway/background_startup.py:1030-1033` `start_proactive_runner`（`evoflow/proactive/runner.py`）；`backend/app/gateway/routers/agents.py`（`list_agents:502`、`create_agent_endpoint:739`、`update_agent:848`、`delete_agent:1137`）为角色配置 CRUD，需登记为「配置入口」而非「执行入口」。
- **旧事实源 / 状态机**：`evoflow_proactive_roles`（`status` = active/paused/stopped/archived、`heartbeat_rrule`、`next_heartbeat_at`、`reports_to`）、`evoflow_proactive_initiatives`（`status`、`round_id`、`goal`、`outcome`、`approval_id`、`execution_thread_id`、`execution_result`）、`evoflow_proactive_approvals`（`status`、`decided_by`、`escalation_level`、`task_id`）、`evoflow_agents`、`evoflow_agent_runtime`（`status`、`current_task_id`、`progress`、`last_heartbeat`）、工作项落点为 `evoflow_collab_tasks`；schema 见 `backend/packages/harness/evoflow/persistence/schema.py:174`、`:162`、`:1175`、`:1147`、`:1117`、`:553`。
- **Runtime 调用点 / 边界**：只记录源码中已经存在且可读取的 Runtime / bridge 接点。2026-09-15 已核实：`backend/packages/harness` 全目录**没有任何** `qagent_runtime` 或 `agentscope` 引用，本域现存 Runtime 接点为 0；旧执行链为 `evoflow/proactive/engine.py`（`ProactiveEngine.think`）→ `evoflow/proactive/execution_bridge.py`（`ExecutionBridge`，路由到 LangGraph lead_agent / supervisor 委派 / 直连 LangGraph）→ `evoflow/proactive/decision_gate.py`（审批）→ `evoflow/proactive/work_items.py`（工作项落为 collab Task）。不得把 AgentScope 类型、私有事件或猜测的 Adapter API 写成事实。
- **允许修改范围**：只写 `artifacts/acceptance/agentscope-runtime/AG-G2-AGENT-001-A01/<attempt>/inventory.md`。
- **禁止触碰**：不得修改 `backend/packages/harness/evoflow/proactive/**`、`admin/employees.py`、`goal_service.py` 或任何 backend 代码、测试、schema、migration、配置；不得触碰 Goal / Chat 实现、Gateway 主干、Runtime 内核或非业务运维范围。
- **验收命令 / 产物**：`rg -n "api/proactive|employees\.|start_proactive_runner|evoflow_proactive_(roles|initiatives|approvals)|ProactiveEngine|ExecutionBridge" backend/packages/harness/evoflow/proactive backend/packages/harness/evoflow/admin backend/app/gateway/router_registry.py backend/app/gateway/background_startup.py`；`inventory.md` 必须列真实入口、事实源、写点、Runtime 接点（可为空但须显式写明为 0）和 A02 是否有证据。
- **停止条件**：只能靠臆造入口 / 字段 / Runtime 语义；需要生产代码或 schema 变更；或发现与正在进行的 Chat 任务冲突。

#### AG-G2-AGENT-002-A01：数字员工 / Agent 对象与 Runtime Run / Event / Result / Cancel 字段级映射

- **母任务 / 状态 / Owner / 时间盒**：`G2-AGENT-002` / `Not Started` / Domain Migration + Runtime / 90m。
- **单一目标**：在 `AG-G2-AGENT-001-A01` 产物基础上，把数字员工旧对象、状态、审批、取消与终态逐字段对照到 Runtime Run / Event / Result / Cancel；未知项与 G0 依赖项必须显式标 `Blocked`，不得补猜。
- **真实入口 / 旧事实源**：沿用 A01 已核实的 `/api/proactive`、`/api/platform` `employees.*`、`start_proactive_runner` 与 `evoflow_proactive_roles` / `_initiatives` / `_approvals`、`evoflow_agents`、`evoflow_agent_runtime`、`evoflow_collab_tasks`；不得扩展扫描范围。
- **可复用的 Runtime 公开契约（只登记，不修改）**：`backend/app/qagent_runtime/service.py` 的 `create_run`(70)、`start_run`(85)、`get_run_status`(301)、`stream_events`(304)、`request_cancel`(324)、`resume_run`(337)、`get_result`(374)；事件词汇 `backend/app/qagent_runtime/events.py:13 EventType` 与 `:29 TERMINAL_STATUSES`；注意 `backend/app/qagent_runtime/contract.py:32 RuntimeContract.create_run` **未声明 `org_id`**，而 `service.create_run` 接受该关键字 —— 该差异只登记为缺口，本卡不改契约。
- **映射范围**：至少覆盖角色状态（active/paused/stopped/archived）、心跳（`heartbeat_rrule` / `next_heartbeat_at`）、initiative 状态与 `round_id`、审批状态（`pending` / decided / escalation）、工作项到 collab Task 的状态、结果与错误引用、幂等键来源。
- **G0 依赖标注（必须逐列写，不得实现）**：审批与 cancel 竞态 → `G0-DEC-001`；心跳调度与多实例下「同一触发窗口只创建一个 Run」的执行所有权 → `G0-DEC-002`；事件字段 / sequence / cursor / gap → `G0-DEC-003`；round 续跑、`recover_persisted_sessions` 与 attempt → `G0-DEC-004`。
- **允许修改范围**：只写 `artifacts/acceptance/agentscope-runtime/AG-G2-AGENT-002-A01/<attempt>/mapping.md`；不修改任何源代码或计划外文档。
- **禁止触碰**：不得新增字段、接口、schema、状态机或 fallback；不得实现 cancel / recovery / claim / cursor；不得触碰 HA、备份、灾备、压测、历史迁移或其他业务域。
- **验收命令 / 产物**：`rg -n "evoflow_proactive_(roles|initiatives|approvals)|heartbeat_rrule|next_heartbeat_at|round_id|approval_id|execution_result|agent_runtime" backend/packages/harness/evoflow/persistence/schema.py backend/packages/harness/evoflow/proactive backend/packages/harness/evoflow/admin`；`mapping.md` 逐字段列出证据文件 / 符号、确定值、未知值、G0 决策依赖和 A03 是否可创建。
- **停止条件**：A01 未完成；任一字段需要臆造；发现必须修改 Runtime / schema / migration 才能闭合；或 G0 语义未冻结导致只能选择实现方案。
- **依赖**：`AG-G2-AGENT-001-A01`。

#### AG-G2-GOAL-001-A01：Goal / 协同入口与 G0 依赖确认

- **母任务 / 状态 / Owner / 时间盒**：`G2-GOAL-001` / `Blocked by G0-DEC-001～004` / Domain Migration + Release / 60m（仅只读盘点 / 依赖确认）。
- **单一目标**：只读盘点 Goal / 协同真实入口、旧事实源、状态 / 审批 / 协同写路径，并逐项确认是否依赖四项未冻结 G0 决策；不设计替代语义。
- **真实入口 / 旧事实源**：2026-09-15 已核实的真实 HTTP 入口为 `backend/app/gateway/routers/goal.py:13`（`APIRouter(prefix="/api/goal")`：`POST /start:160`、`POST /{session_id}/stop:205`、`/pause:230`、`/resume:251`、`/end:272`、`/after-chat-turn:293`、`POST /feedback:457`、`GET /{session_key}/status`、`GET /{session_key}/history`）与 `backend/app/gateway/routers/collab.py:23`（`APIRouter(prefix="/api/collab")`：`GET /threads/{thread_id}/tool-approval/pending:385`、`POST /threads/{thread_id}/tool-approval:405`、`POST /threads/{thread_id}/tool-approval/cancel:735`、`GET|PUT /threads/{thread_id}:776,825`、`GET /tasks/{main_task_id}/subtasks/{subtask_id}/history:880`）；服务实现在 `backend/app/channels/services/goal_service.py`（`_sync_session_from_goal_state:537`、`_run_goal_graph:608`、`ensure_goal_stream_loop:808`、`cancel_current_run:1344`、`pause_goal:1369`、`resume_goal:1389`、`end_goal:1411`、`stop_goal:1434`、`_persist_session_snapshot:1547`、`recover_persisted_sessions:1654`、`apply_user_steering:1923`、`submit_feedback:1965`）。事实源为 `evoflow_goal_sessions`（`goal_status`、`status`、`goal_revision`、`continuation_suppressed`、`step_count`、`last_run_id`、`pending_feedback`、`completion_outcome`，schema `:676`）与 `evoflow_collab_tasks`、`evoflow_collab_subtasks`、`evoflow_collab_peer_messages`、`evoflow_collab_task_execution_history`（schema `:553`、`:503`、`:447`、`:544`），协同唤醒调度在 `backend/packages/harness/evoflow/collab/peer/scheduler.py`（`schedule_peer_wake`、`enqueue_peer_wake_if_busy`、`drain_peer_wake_queue`）；旧状态机为 `backend/packages/harness/evoflow/collab/state_transitions.py`（`can_transition:102`、`validate_transition:116`、`get_allowed_transitions:130`）与 `backend/packages/harness/evoflow/collab/models.py:59 TaskStatus`、`:23 CollabPhase`；审批侧核对 `evoflow_proactive_approvals`、`evoflow_tool_approvals`、`evoflow_tool_approval_grants`、`evoflow_tool_approval_audit`（schema `:1117`、`:1407`、`:1399`、`:1387`）。
- **Runtime 调用点 / 边界**：只读登记真实入口与现有调用点。2026-09-15 已核实：`backend/packages/harness` 全目录**没有任何** `qagent_runtime` 或 `agentscope` 引用，`backend/app/channels/services/goal_service.py` 旧执行链为 `LangGraphClient` / `_run_goal_graph`，本域现存 Runtime 接点为 0；`cancel` 后 approval、claim / 执行所有权、Event v1 / sequence / cursor / gap、Recovery Point / attempt 分别标记 `G0-DEC-001`～`004`，不得生成实现方案。
- **允许修改范围**：只写 `artifacts/acceptance/agentscope-runtime/AG-G2-GOAL-001-A01/<attempt>/dependency-inventory.md`。
- **禁止触碰**：不得实现任务 / 审批 / 协同 Runtime 接入，不改 schema、migration、Runtime、Gateway、前端或 Chat；不纳入 HA、备份恢复、灾备、压测、历史迁移。
- **验收命令 / 产物**：`rg -n "evoflow_collab_|evoflow_(proactive|tool)_approvals|approval|claim|cursor|recovery|attempt" backend`（仅在已定位目录内核对）；产物须给出入口、事实源、写点、G0 依赖和“可继续 / Blocked”结论。
- **停止条件**：任何人要求猜测 G0 语义或创建绕过决策的实现卡；发现必须改 schema / Runtime；或入口 / 事实源无法由源码和现有证据证明。

#### AG-G2-GOAL-002-A01：Goal / 协同对象与 Runtime Run / Event / Result / Cancel 字段级映射（G0 阻塞项只登记）

- **母任务 / 状态 / Owner / 时间盒**：`G2-GOAL-002` / `Not Started（映射本身可执行，实现部分 Blocked by G0-DEC-001～004）` / Domain Migration + Runtime / 90m。
- **单一目标**：在 `AG-G2-GOAL-001-A01` 产物基础上，把 Goal 会话、协同任务 / 子任务、协同消息、审批逐字段对照到 Runtime Run / Event / Result / Cancel；受 G0 影响的列只登记依赖与缺口，不给方案。
- **真实入口 / 旧事实源**：沿用 A01 已核实的 `/api/goal`、`/api/collab`、`goal_service.py` 与 `evoflow_goal_sessions`、`evoflow_collab_tasks` / `_subtasks` / `_peer_messages` / `_task_execution_history`、`evoflow_proactive_approvals`、`evoflow_tool_approvals`；不得扩展扫描范围。
- **可复用的 Runtime 公开契约（只登记，不修改）**：`backend/app/qagent_runtime/service.py` 的 `create_run`(70)、`start_run`(85)、`get_run_status`(301)、`stream_events`(304)、`request_cancel`(324)、`resume_run`(337)、`get_result`(374)；事件词汇 `backend/app/qagent_runtime/events.py:13 EventType`、`:29 TERMINAL_STATUSES`。
- **映射范围**：至少覆盖 `goal_status` / `status` / `goal_revision` / `continuation_suppressed` / `step_count` / `last_run_id` / `pending_feedback` / `completion_outcome`，`TaskStatus`（inbox / pending / planning / planned / executing / paused / reviewed / completed / failed / cancelled）与 `CollabPhase` 的双向映射，`execution_authorized` / `authorized_at` / `authorized_by`，`evoflow_collab_task_execution_history.event_json`，`evoflow_collab_peer_messages.status` / `wake_scheduled` / `answered_at` / `expires_at`，以及 `evoflow_tool_approvals.status` / `evoflow_proactive_approvals.status`。
- **G0 依赖标注（必须逐列写，不得实现）**：cancel 与审批竞态、`cancel_current_run` 后 approval 终态 → `G0-DEC-001`；执行授权（`execution_authorized`）与跨实例执行所有权 → `G0-DEC-002`；事件 / sequence / cursor / gap 与 `event_json` 的对照 → `G0-DEC-003`；`recover_persisted_sessions`、round 与 attempt → `G0-DEC-004`。
- **允许修改范围**：只写 `artifacts/acceptance/agentscope-runtime/AG-G2-GOAL-002-A01/<attempt>/mapping.md`；不修改任何源代码或计划外文档。
- **禁止触碰**：不得新增字段、接口、schema、状态机或 fallback；不得实现 cancel / recovery / claim / cursor；不得改动 `state_transitions.py` 的既有迁移规则；不得触碰 HA、备份、灾备、压测、历史迁移或其他业务域。
- **验收命令 / 产物**：`rg -n "goal_status|continuation_suppressed|execution_authorized|event_json|wake_scheduled|TaskStatus|CollabPhase" backend/app/channels/services/goal_service.py backend/packages/harness/evoflow/collab backend/packages/harness/evoflow/persistence/schema.py`；`mapping.md` 逐字段列出证据文件 / 符号、确定值、未知值、G0 决策依赖和 A03 是否可创建。
- **停止条件**：A01 未完成；任一字段需要臆造；需要改 schema / Runtime / `TaskStatus` 迁移规则；或 G0 语义未冻结导致只能选择实现方案。
- **依赖**：`AG-G2-GOAL-001-A01`；实现侧 `Blocked by G0-DEC-001～004`。

#### AG-G2-ASSET-001-A01：Workspace / 文件 / 资产入口与事实源盘点

- **母任务 / 状态 / Owner / 时间盒**：`G2-ASSET-001` / `Not Started` / Domain Migration / 60m。
- **单一目标**：只读定位真实用户入口、资产引用 / 版本 / 权限写路径及 Runtime 接入边界，不扩展到存储运维或历史迁移。
- **真实入口 / 旧事实源**：以源码实际命中为准；已确认事实源包括 `evoflow_artifacts`、`evoflow_media_assets`、`evoflow_org_artifacts`，对应 schema 证据为 `backend/packages/harness/evoflow/persistence/schema.py:247-255`、`:807-823`、`:986-993`。
- **Runtime 调用点 / 边界**：只读确认资产被哪个真实业务入口引用、是否产生 Run / Result / 外部回执；当前未确认统一设备命令实体、命令表、回执协议或设备身份模型，均必须保持未知。
- **允许修改范围**：只写 `artifacts/acceptance/agentscope-runtime/AG-G2-ASSET-001-A01/<attempt>/inventory.md`。
- **禁止触碰**：不得修改资产 / 存储代码、schema、migration、Runtime、文件上传协议、设备基础设施、前端重构；不得纳入存储 HA、文件灾备、备份恢复、压测、容量或迁移演练。
- **验收命令 / 产物**：`rg -n "evoflow_artifacts|evoflow_media_assets|evoflow_org_artifacts" backend/packages/harness/evoflow/persistence/schema.py backend`；产物须列真实入口、事实源字段（仅引用源码可见字段）、Run / Result 边界和未知项。
- **停止条件**：需要设计统一设备命令 / 回执协议；需要 schema / migration / Runtime 代码；或检索结果不足以证明真实入口。

#### AG-G2-KNOW-001-A01：Knowledge / Memory / Tools / MCP / Channels 入口边界盘点

- **母任务 / 状态 / Owner / 时间盒**：`G2-KNOW-001` / `Not Started` / Domain Migration / 75m。
- **单一目标**：只读确认真实用户业务入口、旧事实源、外部副作用边界及其可证明的 Runtime 接入边界；不重构底层基础设施。
- **真实入口 / 旧事实源**：只记录源码和既有业务证据实际证明的 Knowledge、Memory、Tools、MCP、Channels 入口与记录；当前尚无统一入口 / 事实源 / Runtime 接点的完整证据，未知项不能补写成设计。
- **Runtime 调用点 / 边界**：分别记录检索、记忆读写、工具 / MCP 调用、渠道发送等真实入口是否已通过既有 Runtime bridge；不得假设共享 Adapter、Event、Result 或副作用语义。
- **允许修改范围**：只写 `artifacts/acceptance/agentscope-runtime/AG-G2-KNOW-001-A01/<attempt>/inventory.md`。
- **禁止触碰**：不得修改工具 / MCP / channel adapter、Runtime、schema、migration、凭据、Gateway、前端或其他业务域；不得纳入 HA、备份恢复、灾备、压测、容量、历史迁移。
- **验收命令 / 产物**：使用已定位业务目录执行受控 `rg -n "knowledge|memory|tool|mcp|channel" <已定位目录>`，并在 `inventory.md` 记录实际命中路径、入口、事实源、副作用、权限和 Runtime 边界；禁止全库无界扫描替代证据。
- **停止条件**：没有可证实的真实入口；需要发明统一 Runtime API / 事件语义；需要改底层基础设施；或发现 G0 决策依赖却无法只读标记。

### 2.6.3 已完成不可重派卡片

| 范围 | 状态 | 证据 | 不可重派结论 |
| --- | --- | --- | --- |
| Apps / Workflow 真实入口 Runtime → AgentScope 首闭环及其 A01～A05 验收链 | 已完成；TEST GO，PROD NO-GO | `12fdeb9`、`b8cf3e0`、`bec812e`、`f0a1de0`、`bffbf5c`、`e8a0d13`、`f290af9`；`artifacts/acceptance/agentscope-runtime/AG-G2-APP-009-A01/20260914/handoff-index.md` | 不得重复派 Apps / Workflow A01～A05；PROD NO-GO 不是新增业务实现卡 |
| Chat / Live Run 真实入口 Runtime → AgentScope → SSE / 消息写回 | 已完成 | `3c7aa5d4c0c1c7ed263d7cf285a03441f7da8a4a`；`docs/plan/agentscope-2-g2-chat-entry-map.md`；`docs/plan/agentscope-2-g2-chat-manual-acceptance.md` | 不得重复派 Chat / Live Run A01～A05；不得修改或回退该提交 |
| Automation / Task Center 既有 bridge / opt-in | 已集成但非整域完成 | `433a9df`；三份 Automation 证据文档 | 只保留其作为 A01/A02 盘点输入，不宣称整域迁移完成；不得重派已完成 bridge。其中 **Run 建立与关联（`AG-G2-AUTO-003-B01`）已完成**，`AG-G2-AUTO-003-A01` 只补其未接线的终态回写，不是重派 |
| 数字员工 / Agent、Goal / 协同的旧执行链 | 未迁移（无 Runtime 接点） | 本批 A01 / A02 为只读证据卡；harness 内 `qagent_runtime` / `agentscope` 引用为 0 | 不得把 A01 / A02 当作已接入；A03～A05 未实例化 |

### 2.6.4 被 G0 决策阻塞的卡片 / 后续事项

下列事项受未冻结语义直接影响，本次不实例化实现卡，也不假设方案：

| 事项 | 阻塞决策 | 允许的当前动作 |
| --- | --- | --- |
| Task Center cancel 后 approval 状态与终态回写 | `G0-DEC-001` | 只在 `AG-G2-AUTO-002-A01` 记录字段缺口；不得实现；`AG-G2-AUTO-004-A01` 不得断言该子场景 |
| Task Center / Agent / Goal 执行 claim 与所有权持久化、跨实例触发幂等 | `G0-DEC-002` | 只读盘点现有 claim 线索；不得实现持久化 claim / lease / fencing；`AG-G2-AUTO-003-A01` 明确排除该语义 |
| Event v1、sequence、cursor、gap 的业务映射 | `G0-DEC-003` | 只记录现有事件证据；不得实现协议或补偿语义；`AG-G2-AUTO-003-A01` 不得引入流式帧 / cursor / gap |
| Recovery Point / attempt 与重启恢复 | `G0-DEC-004` | 只记录恢复入口和缺口；不得实现恢复或历史迁移 |
| Goal / 协同 A03～A05 | `G0-DEC-001～004`（按具体字段） | 等 `AG-G2-GOAL-001-A01` / `AG-G2-GOAL-002-A01` 证据与负责人决策冻结后再实例化 |
| Agent / 数字员工 A03～A05 | `G0-DEC-001～004` 且本域 Runtime 接点为 0 | 等 `AG-G2-AGENT-001-A01` / `AG-G2-AGENT-002-A01` 证据与负责人决策冻结后再实例化 |
| Automation A03～A05 中的 Event 流式 / cursor / gap、跨实例幂等、cancel 与 approval 耦合部分 | `G0-DEC-001～003` | `AG-G2-AUTO-003-A01` 只做状态投影与终态回写；其余部分等决策冻结后再实例化 |
| 其他域（Workspace / Asset、Knowledge / Memory / Tools / MCP / Channels）的 A02～A05 | `G0-DEC-001～004`（按具体字段） | 等 A01 证据与负责人决策冻结后再实例化 |

### 2.6.5 未实例化且未擅自拆分的母任务

以下母任务仍保留在第 5 节的总体登记中，但不是可直接派工卡；本次只实例化了 §2.6.2 明确列出的卡片（其余域为 A01 / A02 证据卡，Automation 另含 A03～A05）：

- `G2-AGENT-003`～`G2-AGENT-006`：A01 / A02 已实例化；A03～A05 未实例化，因为 `backend/packages/harness` 内不存在任何 Runtime 接点（已核实为 0），且实现语义受 `G0-DEC-001～004` 阻塞。
- `G2-GOAL-003`～`G2-GOAL-006`：A01 / A02 已实例化；A03～A05 未实例化，需先完成 G0 依赖确认，不绕开 cancel / claim / Event / recovery 决策。
- `G2-AUTO-006`：灰度与观察期未实例化（需租户级开关与观察期 metrics，且跨实例触发幂等未冻结）；`G2-AUTO-003`～`G2-AUTO-005` 本轮只实例化各自的一张 A0x 卡，重启对账、数据对账、Event 流式续读与灰度子步骤仍未实例化。
- `G2-ASSET-002`～`G2-ASSET-006`：真实文件入口、字段和 Runtime 接点未形成证据，不混入存储运维。
- `G2-KNOW-002`～`G2-KNOW-006`：统一真实入口、事实源和外部副作用边界尚无证据。
- 第 5 节中 Apps / Workflow、Chat / 会话的旧母任务行：实际完成状态以本节不可重派清单为准，不得重新派发同一首闭环工作。
- G3/G4 的 HA、多实例、备份恢复、灾备、压测、容量、历史迁移、PG 运维和迁移演练母任务：不属于本轮剩余业务迁移拆卡，保持原阶段登记，不在本节实例化。

## 3. G0：生产正确性底座

### 3.1 已完成或基本完成

| ID | 阶段 | Milestone | 任务 | 状态 | Owner | 依赖 | 工作量 | 修改范围 | 验收标准 | 测试与证据 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| G0-RT-001 | G0 | SDK 基线 | 锁定 AgentScope `2.0.8` 并完成兼容性 probe | Done | Runtime | — | 1d | `backend/requirements*`、兼容性 probe、AgentScope 测试 | 依赖版本可重复安装；关键接口 probe 通过；升级不会静默改变 API | `app/tests/test_agentscope_compat.py`；基线回归 27 passed |
| G0-DB-001 | G0 | PostgreSQL 基础 | 建立 PostgreSQL 基础设施和基础 migration | Done | DB | — | 2d | `backend/migrations/versions/0001_postgres_foundation.py`、`backend/scripts/postgres_verify.py` | disposable verify 数据库可初始化；foundation marker、Alembic revision 和安全校验通过 | `backend/scripts/postgres_verify.py` 的 foundation / migration 检查；foundation 提交 `2dfedec` |
| G0-DB-002 | G0 | Runtime Schema | 创建 Runtime 核心表和索引 | Done | DB | G0-DB-001 | 2d | `backend/migrations/versions/0002_qagent_runtime.py`、Runtime models / repository | 五类核心表、外键、Run sequence 唯一约束和必要索引存在；模型读写测试通过 | `0002_qagent_runtime.py`；Runtime persistence foundation 测试；提交 `2dfedec` |
| G0-SEC-001 | G0 | Security | Bearer 鉴权 | Done | Runtime | — | 1d | Runtime router auth dependency、测试 fixture | 无 Bearer 或无效 Bearer 的所有 Run / Approval 路由返回 401；有效身份可访问授权资源 | `test_qagent_runtime_router.py::test_runtime_routes_require_bearer_auth` |
| G0-SEC-002 | G0 | Security | organization 隔离 | Done | Runtime | G0-DB-002 | 1d | `org_id` 查询条件、router、repository | org A 不能读取、修改或审批 org B 的 Run / Event / Approval；客户端 org 字段不能越权 | `test_run_read_is_rejected_across_organizations`、`test_all_run_and_approval_routes_are_organization_scoped` |
| G0-IDEM-001 | G0 | Idempotency | organization 级幂等 | Done | Runtime | G0-SEC-002 | 1d | `0004_qagent_runtime_org_idempotency.py`、Run 创建路径 | 同 org 重复 key 返回同一 Run；不同 org 可独立使用同一 key；重复审批 / 启动不产生第二条正式记录 | `test_idempotency_keys_are_scoped_by_organization`、duplicate approval / start tests |
| G0-STATE-001 | G0 | State / Transaction | Approval、Run、Error、Event 事务链 | Done | Runtime | G0-DB-002 | 2d | `backend/app/qagent_runtime/repository.py`、`service.py` | 审批决定、Run 状态、错误和 Event 在同一事务边界内提交；注入 Event 失败时全部回滚 | `test_decision_event_failure_rolls_back_approval_run_and_events`；提交 `fce8c62` |
| G0-EVENT-001 | G0 | Event | Runtime Event 基础持久化和 sequence | Implemented | Runtime / SSE | G0-DB-002 | 2d | Event repository、SSE 查询、`qagent_run_events` | 同一 Run 的 sequence 单调且唯一；事件可持久化和回放；正常 SSE / sequence 测试通过 | `test_qagent_runtime.py`、`test_qagent_runtime_router.py` 的 Event / SSE 场景；缺真实 PostgreSQL / gap 证据 |
| G0-REC-001 | G0 | Recovery | 未完成 Run 启动扫描和恢复入口 | Implemented | Runtime | G0-STATE-001 | 2d | startup recovery、resume path、recovery point repository | 服务启动可发现未完成 Run；只恢复属于当前 org 且可恢复的 Run；不创建第二个执行者 | `test_recovery_reclaims_only_expired_execution_claim_and_preserves_org_isolation`；缺真实重启证据 |

### 3.2 待完成、需补证据或待决策

| ID | 阶段 | Milestone | 任务 | 状态 | Owner | 依赖 | 工作量 | 修改范围 | 验收标准 | 测试与证据 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| G0-DB-003 | G0 | PostgreSQL 集成 | 建立真实 PostgreSQL 集成测试环境 | Implemented（已集成） | DB | G0-DB-001、G0-DB-002 | 2d | disposable PostgreSQL、CI / 本地 fixture、`postgres_verify.py` | 真实 PostgreSQL 执行 migration、事务、org 隔离、幂等和两连接并发测试；可重复清理 | 目标命令：`cd backend && .venv/bin/python scripts/postgres_verify.py`；需归档数据库版本、日志和 verdict。**2026-09-14 归并登记：真实并发验收已由 `fadddd0 merge(g0): integrate PostgreSQL concurrency acceptance` 合入集成分支；其 `AG-G0-DB-003-A01`～`A06` 不得重复派工。是否可标 `Done` 需负责人对照该提交的验收记录确认** |
| G0-DB-004 | G0 | Migration 运维 | migration 升级、失败和回滚验证 | Not Started | DB | G0-DB-003 | 2d | `backend/migrations/versions`、migration 验收脚本 | 中断 / 失败 migration 可重试；upgrade / downgrade 结果可解释；旧约束不会残留 | `postgres_verify.py` migration upgrade / downgrade 输出；需新增故障注入记录 |
| G0-DB-005 | G0 | Backup / PITR | PostgreSQL 备份恢复和 PITR 输入确认 | Not Started | DB | G0-DB-003 | 2d | backup / restore runbook、DB 运维配置、证据目录 | 能生成脱敏验证备份；恢复到隔离库后 schema、Run、Event、组织边界一致；形成 RPO / RTO 基线 | 备份文件校验、restore 日志、`db-checks/`、恢复记录；PITR 具体演练在 G3-DB-001 |
| G0-DEC-001 | G0 | Contract Decision | 冻结 cancel 后 Approval 的最终状态 | Blocked | Release / Integration | — | 1d | ADR / Runtime 状态契约，不先改实现 | 明确 Approval 是 `cancelled`、`expired` 或保持批准态；写出取消与批准竞态、审计和重试规则 | 阻塞原因：负责人未拍板；解除条件：ADR / 契约记录签署并补测试向量 |
| G0-DEC-002 | G0 | Contract Decision | 冻结 persistent execution claim 是否为 G1 必选 | Blocked | Release / Integration | — | 1d | G1 部署约束、claim 契约、ADR | 明确 claim、lease、fencing 是否 G1 必选；若不做，明确单 Worker / 不可 HA 限制 | 阻塞原因：生产部署语义未冻结；解除条件：负责人签署并确认 G1 放行标准 |
| G0-DEC-003 | G0 | Contract Decision | 冻结 Event v1 字段、sequence、cursor、gap 语义 | Blocked | Release / Integration + SSE | — | 2d | Event schema、SSE API 文档、测试向量 | 固化 `event_id`、`run_id`、`attempt_id`、`sequence`、`type`、时间、错误 / 结果 / 恢复引用；定义续读、重复、gap 和终态规则 | 阻塞原因：协议未冻结；解除条件：协议评审通过并归档 golden vectors |
| G0-DEC-004 | G0 | Contract Decision | 冻结 Recovery Point 和 attempt 语义 | Blocked | Release / Integration + Runtime | — | 2d | Recovery Point 契约、敏感数据策略、恢复测试向量 | 明确恢复边界、attempt 创建、已提交副作用、Provider 请求引用、版本 / 校验和迟到结果处理 | 阻塞原因：恢复边界未冻结；解除条件：契约签署并形成至少成功 / 失败 / 重启三组向量 |

## 4. G1：第一条真实 AgentScope Runtime 纵向闭环

### 4.1 Adapter 和执行核心

| ID | 阶段 | Milestone | 任务 | 状态 | Owner | 依赖 | 工作量 | 修改范围 | 验收标准 | 测试与证据 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| G1-ADP-001 | G1 | Adapter | 固化 AgentScope Adapter 输入输出边界 | Implemented | Runtime | G0-RT-001、G0-STATE-001 | 2d | `backend/app/qagent_runtime/agentscope_adapter.py`、provider factory | 业务层不导入 AgentScope 私有对象；Adapter 输入 / 输出只使用 QAgent 契约；fake model 可完成测试 | `test_qagent_runtime_agentscope.py` adapter success / event tests；缺真实 provider 证据 |
| G1-ADP-002 | G1 | Adapter | 建立 AgentScope 异常到 QAgent Error 的映射 | Implemented | Runtime | G1-ADP-001 | 1d | Adapter error mapping、Error persistence | Provider、Adapter、超时和未知异常都有稳定 code / message / retryability；错误和 Run 终态一致 | `test_agentscope_execution_failure`；需补真实错误分类证据 |
| G1-EXE-001 | G1 | Execution | Server-side executor 基础执行 | Implemented | Runtime | G1-ADP-001、G0-REC-001 | 2d | server executor、approval → run → result path | 已批准 Run 能进入执行并产生 Result / Event；未经批准不能调用 executor；结果可通过 API 查询 | `test_qagent_runtime_agentscope.py`、router tests；缺生产并发证据 |
| G1-EXE-002 | G1 | Execution | 真实 Provider 配置与调用 | Not Started | Runtime + Integration | G1-ADP-002、G0-DEC-002 | 2d | Provider config、secret injection、真实 sandbox fixture | 使用脱敏真实 Provider / staging endpoint 完成一次 Approved Run 成功执行；请求、响应、费用 / 超时信息可审计 | 需归档 provider 配置版本、Run / Event 导出和脱敏日志 |
| G1-EXE-003 | G1 | Execution | Provider 超时、失败、重试 | Not Started | Runtime | G1-EXE-002、G0-DEC-004 | 2d | retry policy、error mapping、attempt path | Provider 超时和错误按策略重试；重试次数、attempt、最终状态和 Event 一致；不可重试错误不循环 | 需新增真实或可控 Provider failure test、timeline 和最终 verdict |

### 4.2 Claim、并发和副作用安全

| ID | 阶段 | Milestone | 任务 | 状态 | Owner | 依赖 | 工作量 | 修改范围 | 验收标准 | 测试与证据 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| G1-CLM-001 | G1 | Claim | 定义 execution claim 数据和状态转换 | Blocked | Runtime | G0-DEC-002 | 1d | `qagent_runs` claim 字段、状态机契约、ADR | 明确 owner、lease、epoch / fencing、获取 / 释放 / 过期和终态；状态图与数据库字段一致 | 阻塞于 G0-DEC-002；解除后提交状态图、字段说明和测试矩阵 |
| G1-CLM-002 | G1 | Claim | claim 获取和释放事务 | Not Started | Runtime | G1-CLM-001、G0-DB-003 | 2d | repository claim methods、事务边界、worker entrypoint | 两个 worker 并发时只有一个得到有效 claim；正常完成 / 失败 / cancel 都释放或封存 claim | 需真实 PostgreSQL 两连接并发测试、锁等待 / 结果日志 |
| G1-CLM-003 | G1 | Claim | lease 过期和恢复 | Not Started | Runtime | G1-CLM-002、G0-REC-001 | 2d | lease timestamps、startup recovery、reclaim path | worker 崩溃后超过 lease 才能被新 worker 接管；未过期 claim 不被抢占 | 需进程 kill / restart 故障注入、两个 worker timeline 和 DB snapshot |
| G1-CLM-004 | G1 | Claim | fencing token 或等效旧 worker 防护 | Not Started | Runtime | G1-CLM-003 | 2d | result / event commit guards、fencing epoch | 旧 worker 的迟到写入、事件或结果提交被拒绝或标记为 stale；不得覆盖新 attempt / 终态 | 需模拟旧 worker late write 的集成测试和拒绝日志 |
| G1-CLM-005 | G1 | Claim | Provider 调用前 claim 校验 | Not Started | Runtime | G1-CLM-002、G1-CLM-004 | 1d | executor preflight、Provider wrapper | 无有效 claim、claim 过期或 fencing 不匹配时不得调用 Provider | 需可观测 fake Provider call counter + claim race test |
| G1-CLM-006 | G1 | Claim | 结果提交时 claim 校验 | Not Started | Runtime | G1-CLM-004 | 1d | result commit transaction、terminal transition | 迟到结果不能覆盖新 attempt、cancelled 或 completed Run；成功结果与终态原子提交 | 需 stale result integration test、DB 状态和 Event 对照 |
| G1-CLM-007 | G1 | Claim | 并发、崩溃、重复副作用测试 | Not Started | Runtime + Integration | G1-CLM-002～006 | 3d | concurrency / fault test suite、evidence exporter | 覆盖双 worker、kill、重复投递、迟到结果、Provider call count；无重复不可逆副作用 | 需真实 PostgreSQL + controllable Provider + `artifacts/.../G1-CLM-007/` verdict |

### 4.3 Cancel、Recovery、Attempt

| ID | 阶段 | Milestone | 任务 | 状态 | Owner | 依赖 | 工作量 | 修改范围 | 验收标准 | 测试与证据 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| G1-REC-001 | G1 | Cancel | 定义 cancel 合法检查点 | Blocked | Runtime | G0-DEC-001、G0-DEC-004 | 1d | cancel state diagram、checkpoint contract | 明确 queued / running / provider-call / terminal 各检查点能否取消，以及不可中断时的处置 | 阻塞于 cancel / recovery 契约；解除后提交测试向量 |
| G1-REC-002 | G1 | Cancel | 执行中 cancel 状态转换 | Not Started | Runtime | G1-REC-001、G1-CLM-005 | 2d | cancel endpoint、executor checkpoints、Event / Error | cancel 请求经过授权和幂等处理；产生取消 Event、原因和最终状态；已完成结果不被覆盖 | 需 cancel race、provider in-flight、终态保护测试 |
| G1-REC-003 | G1 | Recovery | Recovery Point 持久化 | Blocked | Runtime | G0-DEC-004、G0-DB-003 | 2d | recovery point repository、payload redaction、transaction | 每个可恢复边界保存 `recovery_point_id`、Run / attempt、输入引用、结果 / 副作用引用、版本 / 校验 | 阻塞于 G0-DEC-004；解除后需 DB record + redaction 验收 |
| G1-REC-004 | G1 | Recovery | 新 attempt 恢复执行 | Not Started | Runtime | G1-REC-003、G1-CLM-003 | 2d | resume path、attempt creation、idempotency guard | 恢复创建新 attempt；已完成副作用被识别并跳过；新 attempt 事件与旧 attempt 可区分 | 需 restart / resume integration test 和事件序列 |
| G1-REC-005 | G1 | Recovery | 重启恢复和迟到结果保护 | Not Started | Runtime + Integration | G1-CLM-004、G1-REC-004 | 2d | startup scan、stale result guard、terminal transition | 服务重启后 Run 可恢复；旧 worker 迟到结果不覆盖新 attempt / 终态；恢复动作可审计 | 需非优雅 kill、真实 PostgreSQL、late result timeline |

### 4.4 SSE 和 Event Protocol

| ID | 阶段 | Milestone | 任务 | 状态 | Owner | 依赖 | 工作量 | 修改范围 | 验收标准 | 测试与证据 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| G1-SSE-001 | G1 | SSE / Event | 固化 Event v1 字段和类型 | Blocked | SSE + Runtime | G0-DEC-003 | 2d | Event schema、API contract、adapter event mapping | 字段、类型、错误 / 结果 / 恢复引用、attempt、sequence 和时间精度固定；未知字段兼容规则明确 | 阻塞于 G0-DEC-003；解除后归档 schema 与 golden vectors |
| G1-SSE-002 | G1 | SSE / Event | `after_sequence` 查询和回放 | Implemented | SSE | G0-EVENT-001 | 1d | repository query、SSE route | 指定 Run / org / sequence 后只返回后续事件；顺序稳定；越权查询为空或拒绝 | 现有 SSE / sequence 测试；缺真实 PostgreSQL 证据 |
| G1-SSE-003 | G1 | SSE / Event | 断线续读和重复事件处理 | Not Started | SSE | G1-SSE-001、G1-SSE-002 | 2d | client reconnect contract、server replay、dedupe tests | 客户端使用最后确认 cursor 重连不丢事件；重复投递不造成第二次状态变化 | 需连接中断 / 重连测试、事件导出和客户端结果 |
| G1-SSE-004 | G1 | SSE / Event | sequence gap 检测 | Not Started | SSE | G1-SSE-001 | 1d | event replay validation、error response / recovery action | 检测到 gap 时显式报错或触发补偿；不静默返回不完整事件流 | 需构造缺号数据集、API 响应和服务日志 |
| G1-SSE-005 | G1 | SSE / Event | 多订阅者顺序和终态测试 | Not Started | SSE + Integration | G1-SSE-003、G1-SSE-004 | 2d | multi-subscriber test harness、terminal guards | 多订阅者看到同一 Run 的稳定顺序；终态不可倒退；重复 / 迟到事件可解释 | 需至少 2 个订阅者、并发发布、终态对账报告 |

### 4.5 G1 放行检查

| ID | 阶段 | Milestone | 任务 | 状态 | Owner | 依赖 | 工作量 | 修改范围 | 验收标准 | 测试与证据 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| G1-GATE-001 | G1 | Release Gate | 汇总 G1 纵向闭环并签署放行 | Not Started | Release / Integration | G0-DEC-001～004、G0-DB-003、G1-EXE-002、G1-CLM-007、G1-REC-005、G1-SSE-005 | 1d | release checklist、evidence index、风险登记 | 四项契约冻结；真实 PG / Provider 成功和失败；claim、cancel、restart、recovery、SSE、迟到结果和副作用验收全通过；无未关闭 P0 / P1 | `artifacts/.../G1-GATE-001/verdict.md`；关联所有任务证据和例外签署 |

## 5. G2：按业务域迁移

### 5.1 G2 统一执行规则

每个业务域都必须按以下 6 个独立任务执行，不得只提交一份“域已迁移”结论：

1. 盘点现有入口、数据表、repository、写路径和旧 Runtime 依赖；
2. 映射 QAgent Runtime 对象、状态、事件、幂等键和权限；
3. 接入 AgentScope Adapter 和真实执行入口；
4. 切换正式 PostgreSQL 写入、恢复和事件回放；
5. 完成域测试、回滚测试和数据对账；
6. 灰度切换并完成观察期验收，验证旧链路停止写入。

未被 §2.6 新卡覆盖的 G2 条目仍是未实例化母任务，保持 `Not Started`，不得直接派工；Apps / Workflow 与 Chat / Live Run 的实际状态以 §2.6 的已完成不可重派清单为准。以下 Owner 以角色占位，开始后必须补具体负责人和批次号。

### 5.2 Agent / 数字员工

| ID | 阶段 | Milestone | 任务 | 状态 | Owner | 依赖 | 工作量 | 修改范围 | 验收标准 | 测试与证据 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| G2-AGENT-001 | G2 | Agent 盘点 | 盘点 Agent 入口、表、写路径和 LangGraph 依赖 | Not Started | Domain Migration | G1-GATE-001 | 2d | Agent service、repository、旧 graph / gateway 配置、盘点文档 | 列出所有创建、运行、审批、结果、事件和恢复写路径；每条路径有源表、目标对象和负责人 | `inventory.md`、代码搜索清单、样本数据快照 |
| G2-AGENT-002 | G2 | Agent 映射 | 定义 Agent / 数字员工到 Runtime 对象和事件映射 | Not Started | Domain Migration + Runtime | G2-AGENT-001 | 2d | mapping spec、ID / status / event mapping | Agent ID、Run、Approval、Event、Result、Recovery Point、org 权限和幂等键映射无歧义 | mapping spec、字段级对照表、评审记录 |
| G2-AGENT-003 | G2 | Agent 接入 | 接入 AgentScope Adapter 和 Agent 正式执行入口 | Not Started | Domain Migration + Runtime | G2-AGENT-002 | 3d | Agent service / adapter integration、feature flag | 业务服务只调用 QAgent 契约；成功、失败、取消和恢复均进入统一 Runtime | 域单测、staging Run / Event 导出 |
| G2-AGENT-004 | G2 | Agent 正式写入 | 切换 Agent PostgreSQL 写入、恢复和事件回放 | Not Started | Domain Migration + DB | G2-AGENT-003 | 3d | Agent write path、migration / backfill、recovery hook | 新写入只以 PostgreSQL 为正式来源；重启可恢复；事件可按 cursor 回放；无长期双写 | PG 数据校验、重启记录、旧路径写入监控 |
| G2-AGENT-005 | G2 | Agent 验收 | 完成 Agent 域测试、回滚和数据对账 | Not Started | Domain Migration + Integration | G2-AGENT-004 | 3d | domain tests、reconciliation SQL、rollback script | 数量、主键、org、状态、事件、权限和结果 100% 对账或每个例外有批准处置；回滚可执行 | 域验收矩阵、对账输出、回滚演练记录 |
| G2-AGENT-006 | G2 | Agent 灰度 | Agent 灰度切换和观察期验收 | Not Started | Release / Integration | G2-AGENT-005 | 2d | tenant flag、metrics、cutover checklist | 选定企业 / 租户成功运行观察期；旧链路停止写入；错误率、延迟、恢复和副作用均在阈值内 | 灰度 timeline、metrics 截图 / 导出、签署 verdict |

### 5.3 Apps / 工作流

| ID | 阶段 | Milestone | 任务 | 状态 | Owner | 依赖 | 工作量 | 修改范围 | 验收标准 | 测试与证据 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| G2-APP-001 | G2 | Apps 盘点 | 盘点 Apps / 工作流入口、表、步骤和旧 Runtime 写路径 | Not Started | Domain Migration | G2-AGENT-006 | 2d | Apps / workflow service、repository、graph config | 覆盖触发、步骤、审批、结果、失败、重试、资产和调度入口 | inventory、调用链和样本快照 |
| G2-APP-002 | G2 | Apps 映射 | 定义工作流步骤、Run、Event、attempt 和结果映射 | Not Started | Domain Migration + Runtime | G2-APP-001 | 2d | mapping spec、state / event catalog | 多步骤状态、步骤 sequence、幂等键、失败恢复和 org 权限可重建 | 字段 / 状态 / 事件对照表和评审记录 |
| G2-APP-003 | G2 | Apps 接入 | 接入 AgentScope Adapter 和工作流执行入口 | Not Started | Domain Migration + Runtime | G2-APP-002 | 3d | workflow executor、adapter integration | 每一步都通过 QAgent Runtime 记录；Provider 失败、cancel、resume 不绕过统一状态机 | 域单测、staging 多步骤 Run 证据 |
| G2-APP-004 | G2 | Apps 正式写入 | 切换工作流 PostgreSQL 写入、恢复和回放 | Not Started | Domain Migration + DB | G2-APP-003 | 3d | workflow write path、backfill、recovery | 断点恢复不重复已完成步骤；步骤事件和最终结果可回放；旧 SQLite / graph 不再正式写入 | PG 对账、重启 / resume 日志、旧写入探针 |
| G2-APP-005 | G2 | Apps 验收 | 完成工作流域测试、回滚和数据对账 | Not Started | Domain Migration + Integration | G2-APP-004 | 3d | workflow tests、reconciliation、rollback | 单步、并行 / 串行、失败、重试、取消、恢复和权限全部通过；回滚结果可解释 | acceptance matrix、SQL 对账、rollback verdict |
| G2-APP-006 | G2 | Apps 灰度 | Apps / 工作流灰度和观察期验收 | Not Started | Release / Integration | G2-APP-005 | 2d | tenant flag、metrics、runbook | 灰度租户无未分类 Run、重复副作用或事件 gap；旧链路停止写入 | 灰度证据、告警记录、观察期签署 |

### 5.4 Goal / 任务 / 协同

| ID | 阶段 | Milestone | 任务 | 状态 | Owner | 依赖 | 工作量 | 修改范围 | 验收标准 | 测试与证据 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| G2-GOAL-001 | G2 | Goal 盘点 | 盘点 Goal、任务、协同入口和写路径 | Not Started | Domain Migration | G2-APP-006 | 2d | Goal / task / collaboration services、repositories | 覆盖任务创建、分派、审批、协同事件、完成、取消、超时和通知 | inventory、调用链、样本数据 |
| G2-GOAL-002 | G2 | Goal 映射 | 定义任务状态、Run、Approval、Event 和权限映射 | Not Started | Domain Migration + Runtime | G2-GOAL-001 | 2d | mapping spec、RBAC / org rules | 任务状态与 Runtime 状态合法迁移；协同参与者和 org 权限不可越权 | mapping、状态机和权限评审 |
| G2-GOAL-003 | G2 | Goal 接入 | 接入 AgentScope 执行、审批和协同事件 | Not Started | Domain Migration + Runtime | G2-GOAL-002 | 3d | Goal service、approval integration、adapter | 任务触发 Agent Run；审批、取消、恢复、通知事件进入统一 Event；无框架私有对象外泄 | 域单测、端到端任务样本 |
| G2-GOAL-004 | G2 | Goal 正式写入 | 切换 Goal 正式 PostgreSQL 写入和恢复 | Not Started | Domain Migration + DB | G2-GOAL-003 | 3d | Goal repositories、migration / backfill、recovery | 任务、Run、审批、协同事件和结果可从 PG 恢复；重复触发保持幂等 | PG 对账、恢复演练、幂等报告 |
| G2-GOAL-005 | G2 | Goal 验收 | 完成 Goal 域测试、回滚和数据对账 | Not Started | Domain Migration + Integration | G2-GOAL-004 | 3d | domain tests、reconciliation、rollback | 分派、审批竞态、取消、超时、越权、恢复和通知一致性通过；差异可解释 | acceptance verdict、对账 SQL、回滚记录 |
| G2-GOAL-006 | G2 | Goal 灰度 | Goal / 任务 / 协同灰度和观察期验收 | Not Started | Release / Integration | G2-GOAL-005 | 2d | tenant flag、observability、runbook | 灰度期间无任务状态倒退、重复通知或未知 Run；旧写入路径关闭 | 观察期 metrics、事件导出、签署 verdict |

### 5.5 聊天和会话

| ID | 阶段 | Milestone | 任务 | 状态 | Owner | 依赖 | 工作量 | 修改范围 | 验收标准 | 测试与证据 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| G2-CHAT-001 | G2 | Chat 盘点 | 盘点聊天、会话、消息和流式入口 | Not Started | Domain Migration | G2-GOAL-006 | 2d | chat / session service、message repository、SSE / gateway | 找全会话创建、消息发送、流式输出、重试、编辑、权限和历史读取路径 | inventory、调用链、消息样本 |
| G2-CHAT-002 | G2 | Chat 映射 | 定义会话、消息、Run、Event、cursor 和幂等映射 | Not Started | Domain Migration + SSE | G2-CHAT-001 | 2d | mapping spec、message / event contract | 消息 ID、Run / attempt、sequence、cursor、重复发送和 org 权限语义固定 | mapping、golden conversation vectors |
| G2-CHAT-003 | G2 | Chat 接入 | 接入 AgentScope Adapter 和聊天流式执行 | Not Started | Domain Migration + Runtime | G2-CHAT-002 | 3d | chat service、adapter、SSE bridge | 聊天只依赖 QAgent Event；流式成功、失败、取消、重连和最终消息一致 | chat E2E、SSE reconnect test |
| G2-CHAT-004 | G2 | Chat 正式写入 | 切换会话 / 消息 PostgreSQL 写入和断线恢复 | Not Started | Domain Migration + DB | G2-CHAT-003 | 3d | chat write path、backfill、cursor replay | 重连不丢消息、不产生重复正式消息；会话 / Run 可从 PG 恢复 | PG 对账、断线 timeline、消息 hash 校验 |
| G2-CHAT-005 | G2 | Chat 验收 | 完成聊天域测试、回滚和数据对账 | Not Started | Domain Migration + Integration | G2-CHAT-004 | 3d | chat tests、reconciliation、rollback | 顺序、重复、断线、越权、失败、恢复、历史读取全部通过；异常有处置单 | chat acceptance matrix、对账、rollback verdict |
| G2-CHAT-006 | G2 | Chat 灰度 | 聊天灰度和观察期验收 | Not Started | Release / Integration | G2-CHAT-005 | 2d | tenant flag、SSE metrics、support runbook | 灰度期间消息无丢失 / 重复 / 越权，事件 gap 为零或有自动处置；旧链路停止写入 | 观察期日志、SSE metrics、签署记录 |

### 5.6 自动化和调度

| ID | 阶段 | Milestone | 任务 | 状态 | Owner | 依赖 | 工作量 | 修改范围 | 验收标准 | 测试与证据 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| G2-AUTO-001 | G2 | Automation 盘点 | 盘点自动化、调度、后台任务和触发写路径 | Not Started | Domain Migration | G2-CHAT-006 | 2d | scheduler、background tasks、automation repositories | 覆盖 cron、手动触发、重试、并发、禁用、租户时区和无人值守路径 | inventory、调度表 / 配置快照 |
| G2-AUTO-002 | G2 | Automation 映射 | 定义触发器、Run、attempt、claim、Event 和幂等映射 | Not Started | Domain Migration + Runtime | G2-AUTO-001 | 2d | mapping spec、scheduler contract | 同一触发窗口不重复创建 Run；调度重启和租户隔离规则明确 | mapping、时序图、评审记录 |
| G2-AUTO-003 | G2 | Automation 接入 | 接入 AgentScope Adapter 和调度执行入口 | Not Started | Domain Migration + Runtime | G2-AUTO-002 | 3d | scheduler / worker、adapter integration | 调度创建的 Run 走统一 claim、Event、cancel、resume 和 result path | scheduler E2E、worker logs |
| G2-AUTO-004 | G2 | Automation 正式写入 | 切换自动化正式 PostgreSQL 写入和重启恢复 | Not Started | Domain Migration + DB | G2-AUTO-003 | 3d | scheduler state、backfill、recovery | 调度状态、Run、触发记录可恢复；服务 / worker 重启不会重复执行不可逆动作 | PG 对账、kill / restart timeline、Provider call count |
| G2-AUTO-005 | G2 | Automation 验收 | 完成自动化域测试、回滚和数据对账 | Not Started | Domain Migration + Integration | G2-AUTO-004 | 3d | scheduler tests、reconciliation、rollback | 定时、重入、并发、失败、禁用、时区、恢复和权限场景通过 | 长跑测试、对账、回滚演练 |
| G2-AUTO-006 | G2 | Automation 灰度 | 自动化 / 调度灰度和观察期验收 | Not Started | Release / Integration | G2-AUTO-005 | 2d | tenant flag、scheduler metrics、runbook | 灰度租户触发次数与 Run 数一致；无重复副作用和未知运行任务；旧调度停止写入 | 观察期报告、metrics、签署 verdict |

### 5.7 工作空间、文件、资产

| ID | 阶段 | Milestone | 任务 | 状态 | Owner | 依赖 | 工作量 | 修改范围 | 验收标准 | 测试与证据 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| G2-ASSET-001 | G2 | Asset 盘点 | 盘点工作空间、文件、资产、设备动作和回执写路径 | Not Started | Domain Migration | G2-AUTO-006 | 2d | workspace / asset services、device command / ACK paths | 覆盖资产元数据、版本、引用、上传、下载、处理、设备命令、离线回执 | inventory、资产样本、设备协议依赖清单 |
| G2-ASSET-002 | G2 | Asset 映射 | 定义资产、Run、Event、Result、设备命令和版本映射 | Not Started | Domain Migration + Runtime | G2-ASSET-001 | 2d | mapping spec、asset / command contract | asset / version / locator、org 权限、command_id、idempotency_key、ACK 和 fencing 语义明确 | mapping、字段 / 状态 / 事件评审 |
| G2-ASSET-003 | G2 | Asset 接入 | 接入 AgentScope Adapter、资产处理和设备动作 | Not Started | Domain Migration + Runtime | G2-ASSET-002 | 3d | asset processor、device gateway、adapter integration | 资产处理和设备命令通过统一 Run / Event；不可逆动作有 claim / ACK / 幂等保护 | staging 文件与设备 E2E、回执日志 |
| G2-ASSET-004 | G2 | Asset 正式写入 | 切换资产元数据、版本和回执的 PostgreSQL 写入 / 恢复 | Not Started | Domain Migration + DB | G2-ASSET-003 | 3d | asset repositories、backfill、offline outbox / inbox | 元数据、引用、版本、回执可恢复；本地设备不是唯一正式来源；重复 ACK 不重复副作用 | PG / hash 对账、断线重连 timeline |
| G2-ASSET-005 | G2 | Asset 验收 | 完成资产域测试、回滚和数据对账 | Not Started | Domain Migration + Integration | G2-ASSET-004 | 3d | asset / device tests、reconciliation、rollback | 权限、版本、hash、离线、重复回执、过期回执、设备故障和恢复通过 | asset acceptance、hash / count 对账、rollback record |
| G2-ASSET-006 | G2 | Asset 灰度 | 工作空间 / 文件 / 资产灰度和观察期验收 | Not Started | Release / Integration | G2-ASSET-005 | 2d | tenant flag、device metrics、runbook | 灰度期间无未知资产、重复动作、越权下载或未解释回执；旧写入已关闭 | 设备 / 服务日志、观察期报告、签署 verdict |

### 5.8 知识、记忆、工具、MCP、渠道

| ID | 阶段 | Milestone | 任务 | 状态 | Owner | 依赖 | 工作量 | 修改范围 | 验收标准 | 测试与证据 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| G2-KNOW-001 | G2 | Knowledge 盘点 | 盘点知识、记忆、工具、MCP、渠道和外部连接写路径 | Not Started | Domain Migration | G2-ASSET-006 | 2d | knowledge / memory / tool / MCP / channel services | 列出索引、记忆、工具调用、凭据引用、渠道消息、回调、重试和副作用入口 | inventory、连接 / 凭据引用清单 |
| G2-KNOW-002 | G2 | Knowledge 映射 | 定义检索、记忆、工具调用、渠道消息和 Runtime 事件映射 | Not Started | Domain Migration + Runtime | G2-KNOW-001 | 2d | mapping spec、tool / channel contract | 外部调用、敏感数据、org 权限、幂等、attempt、结果和失败分类明确 | mapping、数据脱敏和权限评审 |
| G2-KNOW-003 | G2 | Knowledge 接入 | 接入 AgentScope Adapter、工具 / MCP / 渠道执行 | Not Started | Domain Migration + Runtime | G2-KNOW-002 | 3d | tool / MCP / channel adapters、provider wrappers | 工具调用和渠道发送通过统一 claim / Event / result；业务层不依赖 AgentScope 私有事件 | staging tool / channel E2E、调用日志 |
| G2-KNOW-004 | G2 | Knowledge 正式写入 | 切换知识 / 记忆 / 工具 / 渠道记录到正式 PostgreSQL 并支持恢复 | Not Started | Domain Migration + DB | G2-KNOW-003 | 3d | repositories、backfill、recovery / replay | 正式元数据、调用记录、结果引用和恢复边界在 PG；外部系统回执可对账；不长期双写 | PG / 外部系统对账、恢复 timeline |
| G2-KNOW-005 | G2 | Knowledge 验收 | 完成知识 / 连接域测试、回滚和数据对账 | Not Started | Domain Migration + Integration | G2-KNOW-004 | 3d | tool / MCP / channel tests、reconciliation、rollback | 检索、记忆、超时、失败、重试、重复发送、越权、凭据脱敏和恢复通过 | acceptance matrix、调用 / 消息对账、rollback verdict |
| G2-KNOW-006 | G2 | Knowledge 灰度 | 知识、记忆、工具、MCP、渠道灰度和观察期验收 | Not Started | Release / Integration | G2-KNOW-005 | 2d | tenant flag、connection metrics、runbook | 灰度期间外部副作用可追踪、无重复发送或越权调用；旧链路停止写入 | 观察期 metrics、外部回执对账、签署 verdict |

### 5.9 G2 总体放行

| ID | 阶段 | Milestone | 任务 | 状态 | Owner | 依赖 | 工作量 | 修改范围 | 验收标准 | 测试与证据 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| G2-GATE-001 | G2 | Domain Gate | 汇总已迁移业务域并确认下一阶段范围 | Not Started | Release / Integration | 各域 `*-006` | 2d | domain gate checklist、migration ledger、风险登记 | 每个已迁移域的源 / 目标数量、org、权限、状态、事件、Run、资产、回滚和旧写入均有证据；未迁移域仍明确为未开始 | migration ledger、对账汇总、域签署 verdict |

## 6. G3：集中生产加固

| ID | 阶段 | Milestone | 任务 | 状态 | Owner | 依赖 | 工作量 | 修改范围 | 验收标准 | 测试与证据 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| G3-LOAD-001 | G3 | Capacity | 基准性能和容量模型 | Not Started | Observability + DB | G2-GATE-001 | 2d | benchmark harness、DB query / index reports、capacity doc | 给出 Run 创建、Event 写入 / 回放、SSE、Provider wait、恢复扫描的吞吐、延迟和容量阈值 | benchmark raw data、p95 / p99、容量模型 |
| G3-LOAD-002 | G3 | Capacity | 长稳和压力测试 | Not Started | Observability + Runtime | G3-LOAD-001 | 3d | load scripts、long-run environment、leak checks | 在目标并发和超额压力下无状态泄漏、事件 gap、重复副作用或不可解释 Run；错误率和恢复时间在阈值内 | 长稳日志、metrics、事件 / DB 对账、verdict |
| G3-CHAOS-001 | G3 | Chaos | Provider、DB、worker 故障注入 | Not Started | Runtime + DB + Integration | G1-GATE-001、G3-LOAD-001 | 3d | fault injection harness、restart scripts、runbook | 覆盖 Provider timeout / 5xx、DB 断连、worker kill、网络抖动；恢复、重试、终态和人工处置可解释 | chaos timeline、故障注入配置、恢复证据 |
| G3-HA-001 | G3 | HA | 多 worker 和 HA 验证 | Not Started | Runtime + DB | G1-CLM-007、G3-CHAOS-001 | 2d | deployment manifest、worker lease / fencing、routing | 多 worker 不重复执行；单实例故障后可接管；旧 worker 写入被拒绝；无单点隐含假设 | HA 演练、claim / fencing logs、结果对账 |
| G3-DB-001 | G3 | Backup / PITR | PITR、RPO、RTO 演练 | Not Started | DB + Release / Integration | G0-DB-005、G3-CHAOS-001 | 2d | backup / restore automation、PITR runbook | 在定义的故障时间点恢复到隔离环境；数据损失符合 RPO；服务恢复符合 RTO；Run / Event / org 完整 | backup manifest、restore log、RPO / RTO report、签署 verdict |
| G3-OBS-001 | G3 | Observability | Metrics、日志、Trace | Not Started | Observability | G2-GATE-001 | 3d | Runtime / worker / SSE instrumentation、dashboards | 可按 org、Run、attempt、worker、Provider 查询成功率、延迟、重试、claim、gap、恢复和副作用指标；敏感数据脱敏 | dashboard export、sample traces、日志字段清单 |
| G3-OBS-002 | G3 | Observability | 告警规则和运行手册 | Not Started | Observability + Runtime | G3-OBS-001 | 2d | alert rules、SLO / thresholds、on-call docs | P0 / P1 场景触发告警；每条告警有处理动作、升级路径和恢复验证 | alert fire evidence、runbook review、演练记录 |
| G3-OPS-001 | G3 | Operations | 生产故障处置 runbook | Not Started | Release / Integration | G3-CHAOS-001、G3-OBS-002 | 2d | incident runbook、manual reconciliation、support checklist | 覆盖未知 Run、重复副作用、Event gap、Provider 失败、DB 恢复、灰度暂停和人工处置；操作不会绕过授权 | tabletop / live drill、处置 timeline、签署记录 |
| G3-GATE-001 | G3 | Release Gate | 生产加固放行 | Not Started | Release / Integration | G3-LOAD-002、G3-HA-001、G3-DB-001、G3-OPS-001 | 1d | G3 gate checklist、risk exceptions | 所有 P0 / P1 可靠性问题关闭；例外有风险、补偿措施、Owner、截止日期；全部演练证据归档 | G3 verdict、风险登记、批准记录 |

## 7. G4：灰度、回滚和旧链路下线

| ID | 阶段 | Milestone | 任务 | 状态 | Owner | 依赖 | 工作量 | 修改范围 | 验收标准 | 测试与证据 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| G4-CANARY-001 | G4 | Canary | 制定企业 / 租户灰度策略 | Not Started | Release / Integration | G3-GATE-001 | 2d | feature flags、tenant cohort、pause criteria | 明确灰度批次、入口、指标、暂停 / 回滚阈值、负责人和通知路径；不允许无监控全量切换 | canary plan、cohort list、批准记录 |
| G4-CANARY-002 | G4 | Reconciliation | 灰度期间数据和事件对账 | Not Started | Release / Integration + DB | G4-CANARY-001 | 3d | reconciliation jobs、event / Run export、support report | 每批次对账正式对象、Run、Event sequence、权限、资产和外部副作用；差异逐项处置 | batch reports、SQL output、异常清单、verdict |
| G4-ROLLBACK-001 | G4 | Rollback | 灰度回滚演练 | Not Started | Release / Integration | G4-CANARY-002、G3-OPS-001 | 2d | rollback scripts、flags、runbook | 达到阈值可暂停新流量并回滚；in-flight Run 分类处理；不可逆副作用不被盲目重放 | rollback timeline、Run 分类表、签署记录 |
| G4-RUN-001 | G4 | In-flight | in-flight Run 处置 | Not Started | Runtime + Release / Integration | G4-ROLLBACK-001 | 2d | Run classifier、manual reconciliation、support tooling | 切流时 queued / running / waiting / terminal Run 全部分类；每个 Run 有继续、取消、恢复或人工处置结论 | Run inventory、处置记录、无未知运行任务证明 |
| G4-MIG-001 | G4 | History Migration | SQLite 历史数据迁移 | Not Started | DB + Domain Migration | G3-GATE-001、G4-CANARY-002 | 3d | SQLite export / import、batch ledger、mapping scripts | 历史数据按批次迁移；主键、org、时间、状态、关系和引用可追踪；原始快照保留 | migration batch ledger、source / target counts、hash checks |
| G4-MIG-002 | G4 | History Reconciliation | 历史数据对账和异常处理 | Not Started | DB + Domain Migration | G4-MIG-001 | 3d | reconciliation SQL、exception workflow | 数量、主键、外键、org、事件和资产引用 100% 对账或每个差异有批准处置；可重复运行 | reconciliation report、exception tickets、sign-off |
| G4-CLEAN-001 | G4 | Cleanup | LangGraph 旧路径清理 | Not Started | Runtime + Domain Migration | G4-RUN-001、G4-MIG-002 | 2d | LangGraph services、SDK、`langgraph.json`、gateway / config | 生产代码、服务、配置、依赖和启动路径不再承载 LangGraph Runtime；只保留批准的历史只读 / 迁移材料 | `rg` 残留扫描、构建 / 启动日志、部署 manifest |
| G4-CLEAN-002 | G4 | Cleanup | SQLite 正式写入路径下线 | Not Started | DB + Domain Migration | G4-MIG-002 | 2d | SQLite repositories、write flags、migration guard | 生产流量不再写 SQLite；PostgreSQL 成为唯一正式读写来源；必要历史副本只读 | write probe、DB audit、配置 diff、运行日志 |
| G4-CLEAN-003 | G4 | Cleanup | 生产残留和回滚检查 | Not Started | Release / Integration | G4-CLEAN-001、G4-CLEAN-002 | 2d | release checklist、dependency scan、rollback archive | 无未批准旧链路、双写、未知 Run、未解释事件 / 数据差异；回滚材料和历史证据可读取 | 残留扫描、最终对账、release verdict |
| G4-GATE-001 | G4 | Final Gate | AgentScope 2.0 Runtime 全量放行 | Not Started | Release / Integration | G4-CANARY-002、G4-ROLLBACK-001、G4-RUN-001、G4-CLEAN-003 | 1d | final gate、ADR completion record、release archive | G0～G3 全部放行；灰度 / 回滚通过；LangGraph / SQLite 旧正式路径下线；只有 AgentScope 2.0 生产 Runtime | 最终 `verdict.md`、ADR 完成记录、证据索引、负责人签署 |

## 8. G1 / G2 / G3 / G4 统一放行检查

任何阶段不得只用“代码已合并”作为放行依据。放行时必须回答：

- 正式事实源是什么，能否从 PostgreSQL 重建状态、事件和结果？
- `org_id`、身份和权限是否由服务端验证，是否有跨 org 负例？
- 重复请求、重复事件、重试、重启和迟到结果是否收敛？
- Provider 调用前后的 claim / lease / fencing 是否可证明，是否有副作用计数？
- cancel、Recovery Point、attempt 和终态倒退规则是否已冻结并测试？
- SSE / Event 是否能按 sequence 续读、发现 gap、处理重复并保持顺序？
- 真实 PostgreSQL、真实 Provider、并发、故障注入、备份恢复和灰度证据是否已归档？
- 任何例外是否都有书面风险、补偿措施、负责人和截止日期？

## 9. 变更记录

| 日期 | 变更 | 证据 / 备注 |
| --- | --- | --- |
| 2026-09-13 | 将原 foundation plan 拆分为路线图与执行计划；登记 G0～G4 任务、状态、Owner、依赖、工作量、修改范围和验收证据 | 基线 Runtime 回归：27 passed；未修改 Runtime 代码、schema 或公共 API |
| 2026-09-14 | 归并 `origin/codex/g2-execution-workbreakdown`（`0d73a1f`）的 G0–G4 拆卡成果：新增 §2.5 G2 波次化拆卡与执行约束、§2.5.4 三张 App 入口 `AG-*` 卡、§2.5.6 G1 验收场景集、§2.5.7 统一回报格式、§2.6 已集成入口状态；更新文档头与 `G0-DB-003` 登记 | 逐项映射见 `agentscope-2-plan-consolidation-notes.md`；未使用 git merge / cherry-pick；未修改任何代码、schema、ADR；未恢复 foundation-plan.md |
| 2026-09-14 | §2.6 状态同步：G2.1-B App Runner 由"未开始"更正为"已实现 / 已集成（TEST GO，PROD NO-GO）"，登记 `b8cf3e0`～`bec812e` 验收链与 `bffbf5c` / `e8a0d13` / `f290af9` 三笔后续修复；`G0-DB-003`、`G0-DEC-001～004` 状态不变（仍待负责人验收 / 冻结） | 同步只覆盖可由提交直接证明的事实；派工通道、放行结论与未验证项以 `AG-G2-APP-009-A01` 交接索引为准 |
| 2026-09-15 | 以 `3c7aa5d4c0c1c7ed263d7cf285a03441f7da8a4a` 为基线，确认 Chat / Live Run 与 Apps / Workflow 真实首闭环已完成且不可重派；新增 Automation A01/A02、Agent A01、Goal A01、Workspace / Asset A01、Knowledge / Memory / Tools / MCP / Channels A01 原子盘点 / 映射卡；明确 G0-DEC-001～004 阻塞项和未实例化母任务；未修改代码、测试、schema、migration。 |
| 2026-09-15 | 剩余业务迁移第一批拆分（Automation / Task Center、数字员工 / Agent、Goal / 协同）：补齐三个域的 A01 已核实锚点；新增 `AG-G2-AGENT-002-A01`、`AG-G2-GOAL-002-A01` 映射卡；对 Automation 新增 `AG-G2-AUTO-003-A01`～`AG-G2-AUTO-005-A01`（只补已实现但零生产调用点的终态回写与既有读取出口，不重派 Run 建立）；Agent / Goal 的 A03～A05 明确不实例化；同步 §2.6.3～§2.6.5 与路线图 §5。侦察证据见 [G2 第一批拆卡侦察与实例化记录](./agentscope-2-g2-batch1-reconnaissance-notes.md)（非权威）。未修改任何代码、测试、schema、migration。 |
