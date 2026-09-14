# ADR-003：QAgent Agent Runtime 与正式运行底座决策

- **状态**：已接受（Accepted）
- **决策日期**：2026-09-12
- **决策人**：QAgent 产品与工程团队
- **关联计划**：[AgentScope 2.0 企业 Runtime 路线图](../plan/agentscope-2-enterprise-runtime-roadmap.md)

## 1. 决策

QAgent 的生产 Agent Runtime 统一为 **AgentScope 2.0**。现有 LangGraph 运行链路完整替换并删除；不保留 LangGraph 作为存量主链、兼容 Runtime 或长期双 Runtime 方案。

客户私有化部署环境中的 **PostgreSQL** 是企业正式数据的唯一来源。Agent 框架状态、本地 SQLite、进程内存、浏览器缓存和本地日志不能成为企业对象、会话、任务、审批、资产或运行状态的唯一记录。

云端 / 企业内网服务负责 24 小时常驻运行、正式记录和恢复；员工本地设备只在授权范围内访问本机文件、NAS、内网系统和桌面工具，并以受控回执同步结果。

## 2. 背景

QAgent 的终极目标是持续沉淀企业资产、形成企业本体，并据此优化企业经营。个人工作台、员工协同和数字员工是现有产品路径；数字员工是现实员工的数字工作分身，需要在企业可控环境中持续工作。

当前实现存在两类底座限制：

- `backend/app/channels/services/goal_service.py`、`backend/langgraph.json`、gateway 启动 / 代理、自动化与后台任务直接依赖 LangGraph 的 client、thread、run 和 checkpoint；
- `backend/packages/harness/evoflow/persistence/db.py` 和 `schema.py` 以 SQLite 承载大量已有业务对象，另有知识向量、观测和 Runtime 状态分散存储。

这不是增加新业务功能，而是对已存在的员工、Agent、Apps / 工作流、自动化、会话、协同、工作空间、知识、记忆、工具连接、审批和运行记录做底层替换与正式数据收口。

## 3. 实施边界

### 3.1 QAgent 的正式业务状态不交给框架

QAgent 业务服务和 PostgreSQL 保存：

- 企业、组织、员工、身份、授权；
- Agent / 数字员工、Apps / 工作流、自动化、模型、技能、工具、MCP、渠道与连接；
- 会话、消息、任务、协同、Run、事件、审批、审计和恢复记录；
- 工作空间、文件 / 产物元数据、知识、记忆、经验和资产关系；
- 本地设备、能力声明、任务投递和同步回执。

AgentScope 只负责执行 Agent / 工作流和生成运行事件。QAgent 保存可恢复的业务状态，并把 AgentScope 事件映射为 QAgent 的 Run、任务、会话、动作和审计记录。

### 3.2 可保留内部替换接口，但不是双 Runtime 架构

为避免业务 API、前端和数据库直接携带框架专属 `thread`、`checkpoint` 或 `session`，QAgent 内部可以保留一个最小运行接口。它服务于本次 LangGraph → AgentScope 2.0 替换及未来框架升级的隔离。

这不表示 LangGraph adapter 会长期存在。替换完成后，生产实现只有 AgentScope 2.0，所有 LangGraph 专属代码、配置和依赖必须删除。

### 3.3 云端与本地边界

- 云端 / 企业内网：QAgent 服务、AgentScope 2.0、PostgreSQL、常驻调度、状态恢复和正式数据；
- 本地设备：经授权的文件和工具动作、加密缓存、断网 Outbox / Inbox 和幂等回执；
- 本地设备不得成为企业工作历史、Agent 定义、会话、任务或资产的唯一副本；
- 文件原件按客户策略留在本机、NAS、对象存储或既有业务系统，PostgreSQL 保存其归属、引用、权限、版本和处理结果。

## 4. 后果

### 获得

- 只有一个生产 Runtime，避免长期双语义、双测试和状态分叉；
- 既有对象从单机 SQLite、框架 checkpoint 和进程状态收口到客户可控的正式数据库；
- 云端可常驻运行，本地仍可受控使用真实文件和内网能力；
- 多员工、多设备、服务重启、任务恢复、审计和私有化部署有统一基础。

### 代价与要求

- LangGraph 不是删包：必须逐项替换 `GoalService`、`langgraph.json`、gateway mount / proxy、后台启动、自动化和无人值守任务；
- 数据迁移必须按现有 `evoflow_*` 表、repository 和 API 做字段级映射、验证、备份和恢复演练；
- 在正式切换前可以做导入校验和只读比对，但不能把 PostgreSQL 与 SQLite 的长期双写当作常态；
- AgentScope 自身升级也不得改变企业正式业务 ID、状态和审计语义；
- 这轮不以新增数字员工、审批、文件整理或经营功能作为交付物，验收对象是已有功能完成底座替换。

## 5. 完成判定

仅当以下条件全部满足，本 ADR 的实施才算完成：

1. 已有 Agent、Apps / 工作流、Goal、自动化、会话和协同主链都由 AgentScope 2.0 运行；
2. LangGraph 服务、`langgraph.json`、SDK、代理、配置和依赖已删除；
3. PostgreSQL 是所有正式企业对象和可恢复运行状态的唯一读写来源；
4. 现有对象的迁移、权限、版本、关系、备份和恢复均已验证；
5. 云端常驻和本地受控执行通过多员工、多设备、断线重连、重复回执和服务重启回归；
6. 私有化部署、升级、备份与恢复可独立完成。
