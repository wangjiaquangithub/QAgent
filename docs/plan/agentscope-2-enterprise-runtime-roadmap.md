# AgentScope 2.0 企业 Runtime 路线图

- 文档角色：总体路线图与架构边界
- 更新日期：2026 年 9 月 13 日
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

**目标**：按固定顺序逐域迁移 Agent / 数字员工、Apps / 工作流、Goal / 任务 / 协同、聊天 / 会话、自动化 / 调度、工作空间 / 文件 / 资产、知识 / 记忆 / 工具 / MCP / 渠道。

**执行单位与顺序**：G2 的工作单位是**任务卡**（一个真实旧入口 → 一次 Runtime 执行 → 一次旧业务记录终态回写），不是业务域母任务。同一入口按「首次闭环 → 状态投影 → 事件 / SSE 兼容 → 业务语义 → 用户可测业务包」依次推进，不同入口的首次闭环可并行；首批并行入口为聊天 / Live Run、App Runner、无人值守 / Task Center（其中聊天 / Live Run 与无人值守 / Task Center 的首次闭环已集成，App Runner 仍待做；卡片明细与证据见执行计划）。**只有该域的入口、状态投影、事件兼容、业务语义和用户可测业务包全部完成，该域才可标记为已迁移**，不得用单个入口的首次闭环代表整域迁移完成。

每个业务域都必须完成：入口和写路径盘点、对象与事件映射、Adapter 接入、正式 PostgreSQL 写入和恢复、域测试 / 回滚 / 对账、灰度观察期验收。任何域切换前都必须具备权限、状态机、事件、幂等、失败恢复、数据对账、回滚和旧链路停止写入证据。

**放行门槛**：该域的正式数据来源、组织隔离、运行状态、事件序列、迁移差异、回滚边界和旧路径写入验证全部可解释；未完成域不阻塞其他域的设计，但不能被标记为已迁移。

### G3：集中生产加固

**目标**：建立基准容量、长稳和压力模型，完成 Provider / DB / worker 故障注入，多 worker / HA、PITR、RPO / RTO、Metrics、日志、Trace、告警和生产 Runbook。

**放行门槛**：所有 P0 / P1 可靠性问题关闭；若存在例外，必须书面记录风险、补偿措施、负责人和截止日期。备份恢复、容量、长稳、Chaos、告警和处置演练证据全部归档。

### G4：灰度、回滚和旧链路下线

**目标**：按企业 / 租户灰度切换，持续对账，演练回滚并处理 in-flight Run；迁移 SQLite 历史数据后，在确认无正式写入残留和无未处置运行任务后下线 LangGraph / SQLite 旧链路。

**放行门槛**：G0～G3 全部放行；灰度和回滚演练通过；数据、事件、权限、运行中任务和不可逆副作用均可解释；旧链路生产配置、依赖和写入路径已清理，且保留必要的历史只读和迁移证据。

## 5. 业务域迁移总体顺序

1. **Agent / 数字员工**：先证明 AgentScope Adapter 与执行核心的通用边界。
2. **Apps / 工作流**：复用 Run、Event、Recovery 和 Provider 失败语义，覆盖多步骤编排。
3. **Goal / 任务 / 协同**：接入任务状态、审批、协同和跨用户授权。
4. **聊天和会话**：处理会话历史、流式事件、断线续读和消息幂等。
5. **自动化和调度**：处理定时触发、重复触发、并发任务和无人值守恢复。
6. **工作空间、文件、资产**：处理资产引用、版本、权限、设备动作和回执。
7. **知识、记忆、工具、MCP、渠道**：在核心运行语义稳定后接入外部连接、检索、记忆和渠道副作用。

顺序是总体建议，不代表可以跳过每个域的六步验收。任何域在正式切换前都必须完成自己的数据、权限、幂等、失败、恢复、对账和回滚证据。

## 6. 责任边界

- **Runtime Owner**：独占 Runtime 核心实现、状态机、事务、claim、Adapter、Event / SSE 后端语义、取消 / 恢复和 G1 证据。
- **DB Owner**：负责 PostgreSQL 环境、migration 验证、索引 / 容量建议、备份恢复和 PITR 输入；不得绕过 Runtime Owner 修改核心数据契约。
- **SSE / Protocol Owner**：负责 Event v1 字段、sequence、cursor、断线续读、gap 和客户端测试向量；协议冻结后由 Runtime Owner 落地服务端语义。
- **Domain Migration Owner**：负责业务域对象 / 入口 / 写路径盘点、映射、迁移批次、对账、回滚和域观察期。
- **Integration / Release Owner**：负责决策冻结、跨边界变更批准、G0～G4 放行、灰度 / 回滚和例外管理。

具体 Owner、依赖和工作量不在本路线图重复维护，以执行计划为准。

## 7. 相关文档

- [AgentScope 2.0 Runtime 执行计划](./agentscope-2-enterprise-runtime-execution-plan.md)
- [Plan 归并说明（G0–G4 拆卡成果 → 唯一权威结构）](./agentscope-2-plan-consolidation-notes.md)
- [阶段 1.5A QAgent Runtime 实现说明](./phase-1.5A-qagent-runtime.md)
- [验收测试矩阵](./agentscope-2-acceptance-test-matrix.md)
- [Runtime 能力清单](./agentscope-2-runtime-inventory.md)
- [PostgreSQL 基础设施 Runbook](./agentscope-2-postgres-foundation-runbook.md)
- [AgentScope SDK 兼容性](./agentscope-2-sdk-compatibility.md)
