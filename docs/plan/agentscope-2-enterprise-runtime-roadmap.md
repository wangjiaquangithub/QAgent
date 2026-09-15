# AgentScope 2.0 企业 Runtime 路线图

- 文档角色：总体路线图与架构边界
- 更新日期：2026 年 9 月 15 日
- 关联决策：[ADR-003：QAgent Agent Runtime 与正式运行底座决策](../adr/003-agent-runtime-and-agentscope-2-adoption.md)
- 执行进度：[AgentScope 2.0 Runtime 执行计划](./agentscope-2-enterprise-runtime-execution-plan.md)
- 现状清单：[Runtime 能力和代码现状](./agentscope-2-runtime-inventory.md)
- 验收矩阵：[AgentScope 2.0 验收测试矩阵](./agentscope-2-acceptance-test-matrix.md)

> 本文件只描述目标、路线、架构原则、阶段边界和放行门槛。任务状态、Owner、工作量、测试命令和完成证据统一维护在执行计划中，避免路线图与实际进度分叉。

## 1. 目标与非目标

### 1.1 目标

1. AgentScope 2.0 成为 QAgent 唯一生产 Agent Runtime；不长期维护 LangGraph 双轨。
2. PostgreSQL 成为企业服务端唯一正式事实源。AgentScope 内部状态、进程内存、本地 SQLite、浏览器缓存和本地日志不能作为企业状态的唯一来源。
3. 保持现有业务入口和业务含义稳定；本项目主要替换运行、存储、恢复、事件和设备连接底座，不借底座迁移新增无关业务范围。
4. 每个已迁移业务域都能够从正式记录恢复，并具备可证明的幂等、授权隔离、防重复执行、取消和恢复语义。
5. 生产切流前具备容量、可靠性、可观测性、灰度和回滚能力；未达到放行门槛的能力不能通过“已接入代码”代替生产验收。

### 1.2 非目标

- 不把最小 Runtime 骨架或离线 fake provider 测试写成“AgentScope 替换完成”。
- 不把 LangGraph 和 AgentScope 作为长期并行生产 Runtime。
- 不在本路线图中维护逐项开发任务、实时状态或每次回归命令；这些内容见执行计划。
- 不让 AgentScope 私有对象、checkpoint、进程内存或 SQLite 记录成为企业正式状态的唯一依据。

## 2. 唯一路线

```text
G0 生产正确性底座
  → G1 第一条真实 AgentScope Runtime 纵向闭环
  → G2 按业务域迁移
  → G3 集中生产加固
  → G4 灰度、回滚和旧链路下线
```

阶段不能跳过：

- G0 先冻结不可返工的状态、事件、取消、claim 和恢复语义。
- G1 在最小真实链路中证明 PostgreSQL、Adapter、Provider、事件、SSE、重启恢复和副作用安全能够闭环。
- G2 复用 G1 的契约逐域迁移，不按“全量一次切换”处理。
- G3 将容量、故障、HA、备份、监控和运行手册集中补齐。
- G4 只在 G0～G3 放行后进行灰度、回滚、历史迁移和旧链路清理。

## 3. 目标架构原则

### 3.1 QAgent 业务状态与 AgentScope 执行状态分离

QAgent 负责保存企业正式业务状态和可审计运行记录，包括组织、身份、授权、Agent、Apps / 工作流、会话、任务、Run、Approval、Event、Error、Result、Recovery Point、资产和设备回执。AgentScope 负责执行 Agent / 工作流和生成运行过程事件。

业务层只能依赖 QAgent 自有的 Run、Event、Result、Recovery Point 和错误契约；AgentScope 类型、生命周期、异常和事件格式集中封装在 Adapter 内。AgentScope 升级不得改变 QAgent 的业务 ID、状态、审计或恢复语义。

### 3.2 PostgreSQL 是正式事实源

Run、状态、事件、结果引用、恢复点、审批、幂等键和业务域正式对象写入 PostgreSQL。进程内存、AgentScope checkpoint、本地缓存、SQLite 和日志只能作为临时执行材料、迁移输入或只读历史，不得作为正式状态唯一来源。

涉及状态变化时，业务状态、Runtime Event、Error / Result 引用和必要恢复信息必须在明确的事务边界内一致提交。任何“状态成功但事件缺失”“事件存在但正式结果不存在”的组合都属于阻断问题。

### 3.3 授权与 organization 隔离在服务端完成

所有读取、写入、恢复、取消、审批和事件回放动作都由服务端根据已认证身份判权，并以 `org_id` 作为隔离边界。不能信任客户端传入的组织、Owner 或 Runtime 上下文，也不能通过 AgentScope 内部对象绕过 QAgent 授权。

### 3.4 幂等与执行所有权

API、Approval → Run 创建、事件落库、结果提交、重试和恢复动作都必须有稳定的幂等键。Worker 在调用 Provider 前必须取得可证明的执行所有权；并发消费、重试或重启不能产生两个有效执行者或重复不可逆副作用。

G1 的生产基线优先采用 PostgreSQL 持久化 claim、lease 和 fencing。若负责人明确选择更弱语义，必须把单 Worker、不可 HA 等部署限制写入契约，不能默认为生产并发安全。

### 3.5 Event 与 SSE 是 QAgent 契约

AgentScope 原始事件经过 Adapter 归一为 QAgent Runtime Event。事件至少需要可关联 Run、attempt、错误、结果和恢复点，并具有每个 Run 单调递增的 sequence、唯一事件 ID、发生时间和可回放语义。

SSE / API 必须支持按 cursor 或 sequence 续读；客户端重连不能因重复投递产生错误状态变化，sequence gap 不能被静默跳过。Event v1 的字段、cursor、gap、重复事件和终态规则必须在 G1 生产实现前冻结。

### 3.6 Cancel、Recovery 和 Attempt

取消只能在合法检查点生效，必须记录原因和事件。恢复必须使用正式 Recovery Point 与新的 attempt 语义，不能重复提交已经完成的副作用，不能用迟到结果覆盖新 attempt 或终态。Recovery Point 的字段、敏感数据处理、版本 / 校验值和恢复边界必须先形成契约。

### 3.7 本地设备与企业服务边界

云端 / 企业内网负责常驻服务、正式数据、调度、恢复和审计；本地设备只在授权范围内访问本机文件、NAS、内网系统和桌面工具，并通过受控的 Outbox / Inbox、命令、ACK 和幂等回执同步结果。设备离线、重连、重复回执和 fencing 规则属于后续业务域 / 生产加固验收的一部分。

### 3.8 不维护 LangGraph 双轨

迁移期间可以保留只读比对、导入校验或受控回滚材料，但不能把 PostgreSQL 与 SQLite 长期双写或把 LangGraph Adapter 作为长期生产架构。G4 在完成灰度、对账、in-flight Run 处置和回滚演练后，删除 LangGraph 运行路径、正式 SQLite 写入路径及其生产配置残留。

## 4. 阶段目标与放行门槛

### G0：生产正确性底座

**目标**：建立不可返工的 Runtime 状态、事务、幂等、鉴权、事件持久化和恢复入口，并冻结四项生产契约：

1. cancel 后 Approval 的最终状态；
2. G1 是否强制持久化 execution claim、lease 和 fencing；
3. Event v1 的字段、sequence、cursor、断线续读和 gap 语义；
4. Recovery Point 与 attempt 的最小字段、恢复边界和副作用规则。

**放行门槛**：契约有负责人签署的 ADR / 契约记录；真实 PostgreSQL migration、事务和并发验证可重复执行；Runtime 核心表和索引可升级、失败可解释、恢复可验证；已有代码的测试证据和缺口在执行计划中登记。

### G1：第一条真实 AgentScope Runtime 纵向闭环

**目标**：完成 Approved Run → execution claim → AgentScope Adapter → 真实 Provider → Event / Result / Recovery Point → PostgreSQL → API / SSE → 重启 / 恢复的第一条生产形态链路。

**放行门槛**：

- 四项 G0 契约已冻结；
- 真实 PostgreSQL 集成测试通过；
- 真实 Provider 至少完成一次成功和一次失败执行；
- claim / lease / fencing 能防止并发执行、旧 worker 迟到写入和重复副作用；
- cancel、restart、recovery、attempt 的状态和证据一致；
- SSE 断线续读、重复事件、sequence gap 和同 Run 顺序测试通过；
- 所有证据归档，未解决 P0 / P1 正确性问题为零。

### G2：按业务域迁移

**目标**：只推进剩余业务迁移；按真实用户入口逐域建立可独立验收的 `AG-*` 原子卡，不把单个 bridge 或单个入口冒充整域生产迁移完成。当前 Apps / Workflow 与 Chat / Live Run 的真实入口 → Runtime → AgentScope 首闭环已完成，均标记为已完成且不可重派；Automation / Task Center 仅部分接入（Run 建立已闭环，终态回写与既有读取出口未接线），其余业务域尚无足够证据证明已接入。第一批拆卡范围见 §5.1。

**执行单位与顺序**：每个尚未完成业务域按 `A01 真实入口/旧事实源盘点 → A02 对象/状态/事件/取消/终态字段映射 → A03 一个最小真实入口 Runtime 首闭环 → A04 定向契约、幂等、终态与失败边界测试 → A05 原页面/API 手工验收与证据归档` 顺序拆卡。A03 只有在 A01/A02 已登记真实文件、调用点、写域、字段和 G0 依赖后才能创建；没有这些证据的母任务只能保留为未实例化，不得让 Agent 自行拆解。

**G0 边界**：`G0-DEC-001`（cancel 后 approval）、`G0-DEC-002`（claim / 执行所有权持久化）、`G0-DEC-003`（Event v1、sequence、cursor、gap）、`G0-DEC-004`（Recovery Point / attempt）未冻结。受其影响的业务事项只做只读盘点或依赖确认并标记 Blocked，不假设语义、不创建绕过决策的实现卡。

本轮明确排除：HA、多实例、leader election、lease / fencing、备份恢复、灾备、压测、容量、历史数据迁移、PostgreSQL 运维、迁移演练、前端重构、Gateway 主干重构、Runtime 内核重构和 LangGraph 主链替换。上述内容不作为业务迁移 `AG-*` 卡片的验收项。

**放行门槛**：每个已迁移域必须有真实入口、正式业务事实源、字段级映射、组织隔离、运行状态、事件 / 结果、幂等、失败边界、旧路径写入结论和原 API / 页面可复验的证据；未满足者不得标记为“已迁移”。

### G3：集中生产加固

**目标**：建立基准容量、长稳和压力模型，完成 Provider / DB / worker 故障注入，多 worker / HA、PITR、RPO / RTO、Metrics、日志、Trace、告警和生产 Runbook。

**放行门槛**：所有 P0 / P1 可靠性问题关闭；若存在例外，必须书面记录风险、补偿措施、负责人和截止日期。备份恢复、容量、长稳、Chaos、告警和处置演练证据全部归档。

### G4：灰度、回滚和旧链路下线

**目标**：按企业 / 租户灰度切换，持续对账，演练回滚并处理 in-flight Run；迁移 SQLite 历史数据后，在确认无正式写入残留和无未处置运行任务后下线 LangGraph / SQLite 旧链路。

**放行门槛**：G0～G3 全部放行；灰度和回滚演练通过；数据、事件、权限、运行中任务和不可逆副作用均可解释；旧链路生产配置、依赖和写入路径已清理，且保留必要的历史只读和迁移证据。

## 5. 业务域迁移总体顺序

当前剩余业务迁移按以下顺序推进；已完成的 Apps / Workflow 与 Chat / Live Run 不再重复派发：

1. **自动化和调度 / Automation / Task Center**：先完成真实入口与旧状态机盘点、字段级映射；只有证据足够且不触碰 G0 未冻结语义时才实例化最小入口闭环。
2. **Agent / 数字员工**：补齐真实入口、旧事实源、会话 / 状态写路径与 Runtime 边界证据，再决定是否进入映射和实现。
3. **Goal / 任务 / 协同**：先完成入口与 G0 依赖确认；cancel、claim、Event、recovery 相关事项须等待对应决策冻结。
4. **工作空间、文件、资产**：先定位真实用户入口、资产事实源、引用 / 权限写路径和 Runtime 边界，不混入存储运维。
5. **知识、记忆、工具、MCP、渠道**：先定位真实业务入口、事实源和外部副作用边界，再决定映射与最小闭环。
6. **Apps / Workflow、Chat / Live Run**：真实入口 → Runtime → AgentScope 首闭环已完成，状态与证据以执行计划 §2.6.1～§2.6.3 为准，不重派 A01～A05。

### 5.1 批次划分与实例化门槛

剩余业务迁移按批次推进，**第一批为 Automation / Task Center、Agent / 数字员工、Goal / 协同**；后续批次按 §5 顺序另行拆分。

实例化门槛（对每个域、每张卡都成立）：

1. **A01 / A02 无条件可实例化**：真实入口、旧事实源、状态机、Runtime 接点的盘点卡，以及旧对象 ↔ Runtime Run / Event / Result / Cancel 的字段级映射卡，都是只读证据卡，不依赖 G0 决策冻结。
2. **A03～A05 需同时满足两条**：A01 / A02 已能写出**真实旧文件**，且该域已存在**可证明的 Runtime 调用点**；否则只能保留为未实例化母任务，不得让 Agent 自行拆解。
3. **受 G0 影响的域最多到 A02**：依赖 `G0-DEC-001`～`004` 的语义（cancel 后 approval、claim / 执行所有权、Event v1 / cursor / gap、Recovery Point / attempt）只登记依赖与缺口；实现卡必须写 `Blocked by G0-DEC-xxx`，不得假设方案、不得绕过决策。
4. **已完成项不可重派**：Apps / Workflow 首闭环与稳定化、Chat / Live Run 首闭环、Automation / Task Center 的 Run 建立与关联均已完成；后续卡只能覆盖其未完成的读写闭环，不能重做已交付部分。

第一批的域级结论：Automation / Task Center 已具备 A01 / A02 证据与既有 Runtime 调用点，因此可实例化到 A05，但实现范围只限**终态回写与既有读取出口**；Agent / 数字员工与 Goal / 协同一律只实例化 A01 / A02，其实现卡等待 Runtime 接点证据与 G0 决策冻结。

顺序是当前剩余工作的阶段级执行建议，不代表可以跳过每个域的 A01～A05 验收顺序。任何域在正式切换前都必须完成自己的数据、权限、幂等、失败、恢复、对账和回滚证据；本轮不实例化 HA、备份恢复、灾备、压测、容量、历史迁移或迁移运维卡片。逐卡字段、允许修改文件、验收命令和停止条件只在执行计划中维护，本路线图不重复。

## 6. 责任边界

- **Runtime Owner**：独占 Runtime 核心实现、状态机、事务、claim、Adapter、Event / SSE 后端语义、取消 / 恢复和 G1 证据。
- **DB Owner**：负责 PostgreSQL 环境、migration 验证、索引 / 容量建议、备份恢复和 PITR 输入；不得绕过 Runtime Owner 修改核心数据契约。
- **SSE / Protocol Owner**：负责 Event v1 字段、sequence、cursor、断线续读、gap 和客户端测试向量；协议冻结后由 Runtime Owner 落地服务端语义。
- **Domain Migration Owner**：负责业务域对象 / 入口 / 写路径盘点、映射、迁移批次、对账、回滚和域观察期。
- **Integration / Release Owner**：负责决策冻结、跨边界变更批准、G0～G4 放行、灰度 / 回滚和例外管理。

具体 Owner、依赖和工作量不在本路线图重复维护，以执行计划为准。

## 7. 变更记录

| 日期 | 变更 |
| --- | --- |
| 2026-09-15 | 同步 Chat / Live Run 与 Apps / Workflow 真实首闭环为已完成且不可重派；将 G2 剩余业务迁移改为基于真实证据的 A01～A05 原子卡顺序，并明确 G0-DEC-001～004 依赖与本轮非业务范围排除。 |
| 2026-09-15 | 新增 §5.1 批次划分与实例化门槛：第一批为 Automation / Task Center、Agent / 数字员工、Goal / 协同；A01 / A02 无条件可实例化，A03～A05 需同时具备真实旧文件与已存在的 Runtime 调用点，受 G0 影响的域最多到 A02。G2 阶段目标同步 Automation 的「Run 建立已闭环、终态回写未接线」现状。逐卡细节仍在执行计划中维护。 |

## 8. 相关文档

- [AgentScope 2.0 Runtime 执行计划](./agentscope-2-enterprise-runtime-execution-plan.md)
- [Plan 归并说明（G0–G4 拆卡成果 → 唯一权威结构）](./agentscope-2-plan-consolidation-notes.md)
- [阶段 1.5A QAgent Runtime 实现说明](./phase-1.5A-qagent-runtime.md)
- [验收测试矩阵](./agentscope-2-acceptance-test-matrix.md)
- [Runtime 能力清单](./agentscope-2-runtime-inventory.md)
- [PostgreSQL 基础设施 Runbook](./agentscope-2-postgres-foundation-runbook.md)
- [AgentScope SDK 兼容性](./agentscope-2-sdk-compatibility.md)
