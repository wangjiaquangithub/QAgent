# AgentScope 2.0 企业 Runtime 基础路线

- 状态：当前可执行路线，待负责人冻结 4 项决策
- 日期：2026 年 9 月 13 日
- 对应产品定义：[QAgent 产品定义 v1.0](../product/product-definition.md)
- 关联决策：[ADR-003：QAgent Agent Runtime 与正式运行底座决策](../adr/003-agent-runtime-and-agentscope-2-adoption.md)
- 配套资料：[Runtime 现状清单](./agentscope-2-runtime-inventory.md)、[验收测试矩阵](./agentscope-2-acceptance-test-matrix.md)

> 本 Plan 只收口执行路线、范围、门槛和责任边界；不新增业务功能，不修改 Runtime、LangGraph、Gateway、SQLite、自动化或 migration 实现。

## 一、当前阶段

### 1.1 已完成事实（不重复调研、不重复验证）

- AgentScope 已锁定 `agentscope==2.0.8`，兼容性 probe 与 pytest 已通过。
- PostgreSQL 基础设施、安全 destructive verification、备份恢复骨架已完成。
- Runtime 已完成 Bearer 鉴权、org 隔离、org 级幂等。
- Approval → Run → Error → Runtime Event 已实现单事务原子化。
- 尚未完成真实 AgentScope Runtime 纵向执行链。

### 1.2 当前未完成的关键缺口

当前不能宣称“AgentScope 替换完成”。必须先完成不可返工的生产正确性底座，再交付第一条真实纵向链路：

- 执行 claim / 防重复执行语义；
- QAgent Runtime Event 与 SSE 的最小契约、顺序和断线续读语义；
- 取消、恢复、重启后的状态机语义；
- QAgent 与 AgentScope 的 Adapter 边界；
- Approved Run 到 `AgentScope Agent.reply(...)` 的真实调用、持久化和恢复闭环。

### 1.3 唯一路线

```text
G0 不可返工的生产正确性底座
  → G1 第一条真实 AgentScope Runtime 纵向闭环
  → G2 按业务域迁移
  → G3 集中生产加固
  → G4 灰度切流、回滚和旧链路下线
```

G1 可以立刻开工，但 G1 的生产契约和验收必须以“待负责人冻结的决策”作为前置门槛；在冻结前只能搭建不锁死策略的边界和测试骨架，不得通过隐含默认值改变四项决策。

## 二、目标

1. AgentScope 2.0 成为 QAgent 唯一生产 Agent Runtime；不长期维护 LangGraph 双轨。
2. PostgreSQL 成为企业服务端唯一正式事实源。AgentScope 内部状态、进程内存、本地 SQLite、浏览器缓存和本地日志均不能作为企业状态唯一来源。
3. 现有业务入口和业务含义保持不变；底座替换只改变运行、存储、恢复、事件和设备连接方式。
4. 每个已迁移业务域都能从正式记录恢复，并具备可证明的幂等、防重复执行、授权隔离、取消和恢复语义。
5. 生产切流前完成必要的容量、可靠性、可观测性、灰度和回滚准备；可后置的工作不得被误写成 G1 前置。

## 三、范围与能力归属

### 3.1 必须随着功能迁移完成的能力

以下能力是每个迁移业务域的“随迁门槛”，不能留到全部功能迁完后再补。G1 必须先在最小真实链路中证明，G2 必须按域复用并通过：

| 能力 | 随功能迁移必须达到的结果 |
| --- | --- |
| PostgreSQL 事实源 | Run、状态、事件、结果、恢复点以及该业务域的正式对象写入 PostgreSQL；框架内部 checkpoint / 内存不得成为唯一依据。 |
| 鉴权与 org 隔离 | 所有读写和恢复动作服务端判权，按 `org_id` 隔离；不能通过客户端字段或 Runtime 内部上下文绕过。 |
| 状态机 | Approval、Run、Error、完成 / 取消 / 恢复等状态有明确合法迁移；拒绝终态倒退和未经授权的隐式继续。 |
| 原子事务 | 业务状态、QAgent Runtime Event、错误 / 结果引用及必要的恢复信息按既定事务边界一致提交；不得出现“状态成功但事件 / 结果缺失”。 |
| 幂等 | API、Approval → Run 创建、事件落库、结果提交和可重试动作有稳定幂等键；重复请求不会产生重复正式记录或重复副作用。 |
| 执行 claim / 防重复执行 | Worker 在真正调用 Provider 前取得可证明的执行所有权；重启、重试、并发消费不能让同一 Run 产生两个执行者。是否必须持久化 claim 见第六节。 |
| QAgent Runtime Event / SSE 协议 | AgentScope 原始事件经过 Adapter 归一为 QAgent Runtime Event；API / SSE 可读、可按序回放，并能处理断线续读、重复投递和缺口。 |
| 取消和恢复语义 | 取消只在合法检查点生效并留下证据；恢复使用正式 Recovery Point / 新 attempt 语义，不重复执行已提交副作用，不覆盖已有结果。 |
| AgentScope Adapter 边界 | 业务层只依赖 QAgent Run、Event、Result、Recovery Point 等内部契约；AgentScope 类型、生命周期和异常映射集中在 Adapter 内。 |

### 3.2 可在核心功能迁完后集中完成的能力

以下工作可以在 G2 核心业务域迁完后集中做，但**生产切流（G4）前必须全部完成或有负责人批准的书面例外**：

- 压测、容量模型、容量基线与扩缩容策略；
- 长稳 / soak test；
- 全量 Chaos 与跨组件故障注入；
- HA、Worker 横向扩展、多地域部署；
- PITR、隔离环境备份恢复实测与 RPO / RTO 达标；
- 完整监控、Trace、指标、日志关联、告警和告警值班；
- 企业 / 租户灰度策略、切流、回滚演练和 in-flight Run 处置；
- 历史 SQLite 正式数据迁移、全量对账和迁移收尾；
- 最终 LangGraph 清理，包括服务、Gateway 挂载 / 代理、配置、依赖、镜像和只服务旧链路的测试。

“可后置”只表示不阻塞 G1 / G2 的核心开发，不表示可以带着缺口进入生产切流。

### 3.3 不在本 Plan 范围内

- 新增数字员工管理、审批产品、文件自动整理、经营分析或新的协同业务流程；
- 设备迁移；
- G1 阶段删除 LangGraph；
- G1 阶段历史 SQLite 全量迁移；
- 全业务同时迁移；
- G1 阶段 HA、压测、全量 Chaos；
- 修改 Runtime、LangGraph、Gateway、SQLite、自动化或 migration 代码（本 Plan 交付只改本文件）。

## 四、实施路线与门槛

### G0：不可返工的生产正确性底座

**目标：** 把任何业务域都必须依赖的事实源、授权、状态、事务、幂等、执行所有权、事件协议、取消 / 恢复和 Adapter 边界冻结为可复用底座。

**已纳入 G0 的基线：** AgentScope 2.0.8 probe / pytest、PostgreSQL 基础设施与安全验证、备份恢复骨架、Bearer 鉴权、org 隔离、org 级幂等、Approval → Run → Error → Runtime Event 单事务原子化均按已完成事实处理，不重复立项。

**G0 必须收口：**

1. 明确 Run / Approval / Error / Result / Recovery Point / Runtime Event 的状态机和合法迁移；
2. 冻结执行 claim 的持久化与部署约束；
3. 冻结 QAgent Runtime Event、sequence、cursor 和断线续读契约；
4. 冻结取消、恢复、重启和新 attempt 的语义；
5. 冻结 AgentScope Adapter 的输入、输出、异常和生命周期边界；
6. 为以上契约提供最小 PostgreSQL 持久化和并发 / 重启测试入口，不扩展到全业务域。

**G0 退出门槛：** 四项待决策均由负责人拍板并记录；上述契约能被 G1 直接调用；任一重复执行、终态倒退、结果覆盖、越权读写或事件顺序不确定，均不得进入 G1 验收。

### G1：第一条真实 AgentScope Runtime 纵向闭环

**目标：** 只交付第一条真实、可恢复、可读回放的 AgentScope Runtime 主链，证明生产正确性底座不是 mock 或空壳。

**精确范围：**

```text
Approved Run
  → AgentScope Agent.reply(...)
  → Result / Event / Run 状态 / Recovery Point 持久化
  → API / SSE 可读与按序回放
  → Provider 失败、取消、重启场景下不重复执行、终态不倒退、结果不覆盖
```

**G1 实施要求：**

- 从已批准的 Run 读取正式输入、Agent 配置和授权上下文；
- 由 QAgent Adapter 调用真实 `AgentScope Agent.reply(...)`，不得以 LangGraph、伪造结果或仅进程内函数替代；
- 将 AgentScope 事件归一为 QAgent Runtime Event，并与 Run 状态、错误、结果引用、Recovery Point 按事务边界落 PostgreSQL；
- API / SSE 只暴露 QAgent 标识和契约，支持按 `sequence` 读取、断线续读和重复消费收敛；
- Provider 失败可分类并进入合法状态；取消只在定义的安全检查点生效；服务重启后从正式记录恢复；
- 证明同一 Run 不会并发重复执行，终态不会被旧 attempt 改回，已提交结果不会被迟到结果覆盖。

**G1 明确不做：**

- 设备迁移；
- 删除 LangGraph；
- 历史 SQLite 全量迁移；
- 全业务同时迁移；
- HA；
- 压测；
- 全量 Chaos。

**G1 退出门槛：** 最小真实 Agent 链路通过成功、Provider 失败、取消、服务重启、重复触发 / 重复回执、断线续读、越权访问和事务失败场景；每个场景均能提供 Run / Event / Result / Recovery Point 证据。G1 通过后才能开始 G2 按域迁移。

### G2：按业务域迁移

**目标：** 以 G1 的底座和契约为唯一模板，逐域迁移现有功能，不做全业务同时迁移。

**迁移顺序：** 由负责人根据风险和依赖冻结批次；默认先迁移能复用 G1 Runtime 主链且副作用可控的域，再迁移依赖本地设备、复杂资产或长任务的域。候选域来自 Runtime inventory，至少按以下边界拆分：

1. Agent / 数字员工定义与运行配置；
2. Apps / 工作流与 revision / run；
3. Goal、任务、TODO 与员工协同；
4. 聊天、线程、会话和流式恢复；
5. 自动化、计划与调度；
6. 工作空间、上传、文件、资产与产物；
7. 知识、记忆、工具、MCP、渠道和连接器。

**每个业务域的完成条件：**

- PostgreSQL 正式落点、授权 / org 隔离、版本和来源关系明确；
- 该域的 Run、Event、Result、Recovery Point 接入 G1 契约；
- 取消、恢复、幂等、claim / 防重复执行和重启恢复通过该域故障场景；
- 迁移批次可重跑、可对账、可暂停和可回退；
- 旧链路仍保留到 G4，不在单域完成时删除；
- 该域完成后才能进入下一域或扩大灰度范围。

**G2 退出门槛：** 核心业务域全部迁移并通过对应验收矩阵；不存在未分类的运行中任务、重复副作用、越权成功、未知来源资产或未解释的数据差异；G3 所需的容量和可观测性输入齐备。

### G3：集中生产加固

**目标：** 在核心业务域已迁完后，集中完成不影响 G1 纵向闭环、但决定生产可靠性的横切能力。

**工作包：**

- 压测、容量、长稳和扩缩容基线；
- 全量 Chaos、服务 / 数据库 / Provider / 网络故障组合演练；
- HA、Worker 扩展、多地域（若部署形态需要）；
- PITR、隔离环境备份恢复和 RPO / RTO 实测；
- 完整监控、Trace、日志关联、指标、告警和处置 runbook；
- 灰度观测指标、回滚条件、回滚负责人和 in-flight Run 分类；
- 历史 SQLite 正式数据迁移、全量对账；
- LangGraph 清理门、依赖 / 镜像 / 启动 / 主链扫描。

**G3 退出门槛：** 所有生产切流前置项已完成，或每项书面例外均有风险、补偿措施、负责人和截止日期；任何 P0 正确性问题不能以“后续加监控”放行。

### G4：灰度切流、回滚和旧链路下线

**目标：** 在生产正确性和生产加固均达标后，按企业 / 租户 / 业务域灰度切换，保留可执行回滚，再下线旧链路。

**切流顺序：**

1. 冻结窗口和 in-flight Run 分类；
2. 小范围企业 / 租户灰度，核对 Run、Event、Result、Recovery Point 和副作用账本；
3. 达到灰度观察门槛后扩大范围；
4. 触发回滚条件时停止扩大切流，按 Run 分类执行回滚或人工处置，不跨 Runtime 强行重放不可逆副作用；
5. 完成全量对账、备份恢复确认和负责人签署后，删除 LangGraph 运行链路和 SQLite 正式写入路径；
6. 保留必要的历史只读 / 迁移证据，不保留第二套生产 Runtime。

**G4 放行门槛：** G0～G3 门槛全绿；灰度和回滚演练完成；生产环境无未批准 LangGraph 残留；正式数据、运行中任务、权限、事件序列和副作用均可解释、可追溯、可恢复。

## 五、验收与证据

### 5.1 G1 最小验收集

| 场景 | 必须证明 |
| --- | --- |
| 成功执行 | Approved Run 经真实 `AgentScope Agent.reply(...)` 产生结果；Run 状态、Result、Event、Recovery Point 均可追溯。 |
| Provider 失败 | 失败分类、错误事件和 Run 终态一致；可按合法策略重试或进入人工处置；不会重复副作用。 |
| 取消 | 取消请求按冻结的状态机和安全检查点处理；不会把已完成结果覆盖为旧状态。 |
| 服务重启 | 重启前后的 Run / Event / Recovery Point 可从 PostgreSQL 恢复；不创建第二个执行者。 |
| 重复触发 / 回执 | 幂等键和 claim 使重复请求、迟到事件、重复回执收敛，不重复执行或覆盖结果。 |
| SSE 断线 | 客户端可用 cursor / sequence 续读；事件顺序可验证；缺口可发现而非静默跳过。 |
| 授权 | 跨 org 或无权限访问被拒绝；服务端不信任客户端传入的 org / owner 上下文。 |

### 5.2 G2～G4 统一放行原则

- 所有迁移对象的数量、主键、外键、org 归属、权限和版本校验 100% 通过，或每个例外有批准的处置单；
- 不存在未分类运行中任务、未知来源资产、重复副作用、越权成功或未解释的迁移差异；
- 备份恢复、PITR、容量、长稳、Chaos、监控、Trace、告警、灰度和回滚证据在 G4 前归档；
- 验收证据至少关联 `run_id`、`event_id`、`attempt_id`、`recovery_point_id` 或迁移批次 ID；
- 自动恢复、可重试等待、人工处置和阻断性错误必须分开记录，不能用“最终看起来正常”替代状态和证据。

## 六、待负责人冻结的决策

以下四项是 G0 的决策门。G1 可以立刻开工做边界、持久化骨架和非策略依赖的测试，但在 G1 首个生产实现 / 验收前必须由负责人冻结并写入对应契约。

1. **Cancel 发生后 Approval 的状态**
   - 需要拍板：Cancel 是否使 Approval 进入 `cancelled`、`expired`，或保持原批准态并仅终止 Run；不同选择对审计、重试和恢复的影响必须写死。
   - 建议基线：取消只改变允许取消的 Run / attempt；Approval 是否终止单独定义，不允许由 Worker 隐式推断。

2. **G1 是否必须做持久化 execution claim**
   - 需要拍板：claim 是否以 PostgreSQL 持久记录 / lease / fencing 作为 G1 必选。
   - 建议基线：必须持久化。若负责人明确选择不做，G1 必须写死为单 Worker、不可横向扩展、不可 HA 的部署限制，并将该限制作为 G4 前的阻断项，而不是默认允许并发部署。

3. **QAgent Runtime Event 最小字段、sequence、cursor / 断线续读规则**
   - 需要拍板：最小字段集合、每个 Run 的单调 `sequence`、事件唯一 ID、cursor 表示方式、重放范围、重复事件和 sequence gap 的处理。
   - 建议基线：事件至少包含 `event_id`、`run_id`、`attempt_id`、`sequence`、`type`、`occurred_at`、状态 / 错误摘要和结果 / 恢复点引用；cursor 表示最后已确认的 sequence，续读返回 `sequence > cursor`，发现缺口必须显式报错或进入补偿，不得静默跳过。

4. **Recovery Point 在 G1 的最小持久化字段**
   - 需要拍板：恢复点如何标识可恢复边界，以及哪些输入、输出、Provider 请求和校验信息必须保存。
   - 建议基线：至少包含 `recovery_point_id`、`run_id`、`attempt_id`、创建时间、可恢复状态 / 检查点位置、输入快照引用、已提交结果 / 副作用引用、Provider 请求引用、恢复策略、版本 / 校验值；敏感内容按既有密钥与脱敏策略保存。

## 七、并行协作与责任边界

### 7.1 Runtime 主 Agent（独占写域）

- 独占 `qagent_runtime` 和 Runtime migrations 的设计、实现、测试与变更；
- 负责 G0 状态机、事务、幂等、execution claim、Adapter、Runtime Event / SSE 后端语义、取消 / 恢复和 G1 真实纵向闭环；
- 负责 Runtime 核心文件的最终一致性和 G1 证据归档；
- 任何边车不得直接修改 Runtime 核心文件、Runtime migrations 或替 Runtime 主 Agent 做策略决策。

### 7.2 PostgreSQL 边车

- 可并行提供 PostgreSQL 基础设施、schema 设计建议、索引 / 性能建议、备份恢复与运维输入；
- 可提交设计、fixture、对账 SQL 草案和验证说明；
- 不得修改 `qagent_runtime` 核心实现或 Runtime migrations；需要变更 Runtime 数据契约时提交建议，由 Runtime 主 Agent 统一落地。

### 7.3 SSE 契约边车

- 可并行定义 Runtime Event 字段、sequence、cursor、断线续读、错误码和客户端兼容性建议；
- 可提供契约样例、回放测试向量和验收说明；
- 不得直接修改 Runtime 核心文件；最终协议由负责人冻结，Runtime 主 Agent 实现服务端语义。

### 7.4 业务迁移设计边车

- 可按业务域设计对象映射、迁移批次、对账规则、旧链路 / 新链路切换和域验收样本；
- 可并行准备 G2 的迁移计划和回滚清单；
- 不得改 Runtime 核心文件、Runtime migrations、Gateway、LangGraph、SQLite 或自动化实现。

### 7.5 集成与放行

- 负责人负责冻结四项决策、批准跨边界变更、确认 G0～G4 门槛和 G4 切流 / 回滚；
- Runtime 主 Agent 负责将已冻结契约接入主链；边车只提供输入，不绕过独占写域；
- 所有生产切流前的例外必须有书面风险、补偿措施、负责人和截止日期。

## 八、当前可执行工作清单

1. 负责人冻结第六节四项决策，并将结果写入 G0 契约记录。
2. Runtime 主 Agent 在不引入 LangGraph 双轨的前提下，完成 G0 剩余底座和 G1 最小真实链路。
3. SSE 契约边车并行提交字段、sequence、cursor 和断线续读测试向量；PostgreSQL 边车并行提交索引 / 运维输入；业务迁移边车按域提交 G2 批次草案。
4. G1 通过后，按 G2 域顺序逐域迁移；每域完成随迁能力和证据后再扩大范围。
5. 核心域迁完后进入 G3，集中完成生产加固；未完成 G3 不得进入 G4。
6. G4 按企业 / 租户灰度切流，执行回滚演练和 in-flight Run 处置；全部门槛通过后才下线旧 LangGraph / SQLite 正式链路。
