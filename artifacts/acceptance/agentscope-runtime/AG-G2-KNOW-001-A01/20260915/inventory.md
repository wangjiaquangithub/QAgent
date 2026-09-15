# AG-G2-KNOW-001-A01 — Knowledge / Memory / Tools / MCP / Channels 真实入口与事实源盘点

- 卡片：`AG-G2-KNOW-001-A01`（母任务 `G2-KNOW-001`）
- 日期 / attempt：2026-09-15 / `20260915`
- 起始 HEAD：`5a2b8ca1c4dfc6ee502d9f993a4bc3a6ab6bfff8`（分支 `codex/agentscope-runtime`）
- 验收命令（卡片原文的受控形式）：在**已定位业务目录**内执行
  ```
  rg -n "knowledge|memory|tool|mcp|channel" \
     backend/app/gateway/routers/{knowledge,knowledge_owned,knowledge_vaults,memory,task_memory,mcp,mcp_server,channels,tools,skills}.py
  ```
  原始输出：`acceptance-rg.txt`（525 行，与本文件同目录）。
  未做全库无界扫描。
- 本卡**只读**：未修改工具 / MCP / channel adapter、Runtime、schema、migration、凭据、Gateway、前端或其他业务域；
  未纳入 HA、备份恢复、灾备、压测、容量、历史迁移。**未发明统一 Runtime API / 事件语义**。

---

## 1. 结论先行

| 问题 | 结论 |
|---|---|
| 真实入口是否可证明？ | **是**。§2 定位 **10 个路由模块、149 个端点** |
| 旧事实源是否统一？ | **否**。SQLite 主库有 8 张相关表，但**知识库不走主库** —— 它是 vault 文件系统 + 可选向量索引（§3.3）。这是本卡最重要的结构发现 |
| 本域 Runtime 接点是多少？ | **0**。10 个路由模块对 `qagent_runtime` / `agentscope` 的引用数为 **0** |
| 外部副作用边界是否可证明？ | **部分**。§4 列出 3 类可证明的外部副作用（渠道外发、MCP 子进程、工具审批执行）；**凭据存储的具体形态未展开**（§7） |
| 是否应实例化 A02？ | **否**（§8）。五个子域没有共同对象；且知识库事实源形态未定 |

---

## 2. 真实用户入口（10 个模块 / 149 个端点）

| 子域 | 模块 | prefix | 端点 | 鉴权（源码可见） |
|---|---|---|---|---|
| Knowledge | `routers/knowledge.py:27` | `/api/knowledge` | 24 | `Depends(_org_admin_dep)`（`knowledge.py:30`） |
| Knowledge（自有） | `routers/knowledge_owned.py:27` | `/api/knowledge/owned` | 46 | — |
| Knowledge（Vault） | `routers/knowledge_vaults.py:18` | `/api/knowledge/vaults` | 20 | — |
| Memory | `routers/memory.py:20` | `/api` | 20 | — |
| Task detail / memory | `routers/task_memory.py:15` | `/api/task-detail` | 7 | — |
| MCP | `routers/mcp.py:12` | `/api` | 4 | — |
| MCP server | `routers/mcp_server.py:34` | 无前缀 | 2 | — |
| Channels | `routers/channels.py:16` | `/api/channels` | 14 | — |
| Tools / Capability bus | `routers/tools.py:75` | `/v1` | 4 | `Depends(verify_capability_bearer_token)`（`tools.py:79`） |
| Skills | `routers/skills.py:20` | `/api` | 8 | — |

### 2.1 关键端点（按子域）

**Channels（`/api/channels`，14 端点）**——本域外部副作用最集中处：

| 方法 | 路径 | 行 |
|---|---|---|
| GET | `/`（`ChannelStatusResponse`） | `:203` |
| GET | `/push-targets` | `:223` |
| POST | `/{name}/restart` | `:232` |
| POST | `/{name}/enable` | `:251` |
| GET | `/{name}/config` | `:272` |
| PUT | `/{name}/config` | `:286` |
| POST | `/feishu/push` | `:305` |
| POST | `/feishu/registration/begin` | `:347` |
| GET | `/feishu/registration/{session_id}/poll` | `:374` |
| POST | `/feishu/registration/{session_id}/apply` | `:402` |
| POST | `/weixin/registration/begin` | `:470` |
| GET | `/weixin/registration/{session_id}/poll` | `:494` |
| POST | `/weixin/registration/{session_id}/apply` | `:523` |

**MCP（`/api`，4 端点）**：`GET|PUT /mcp/config`（`:125`、`:185`）、`GET /mcp/market/search`（`:248`）、`POST /mcp/market/install`（`:269`）。

**Tools / Capability bus（`/v1`，4 端点）**：`GET /tools`（`:143`）、`GET /tools/{name}`（`:174`）、`GET /tools/{name}/stream`（`:304`）、`GET /openapi.json`（`:337`）。

**MCP server（无前缀，2 端点）**：`mcp_server.py:34` 声明 `APIRouter(tags=["mcp-server"])`，是本平台**对外暴露为 MCP 服务端**的面（与 `mcp.py` 的"作为客户端连接外部 MCP"方向相反）。

---

## 3. 旧事实源

### 3.1 SQLite 主库（`backend/packages/harness/evoflow/persistence/schema.py`）

| 子域 | 表 | schema 行 |
|---|---|---|
| Channels | `evoflow_channel_bindings` | `:300` |
| Channels | `evoflow_channel_configs` | `:308` |
| Channels | `evoflow_channel_push_log` | `:315` |
| MCP | `evoflow_mcp_servers` | `:792` |
| Memory | `evoflow_memory` | `:843` |
| Memory | `evoflow_memory_facts` | `:850` |
| Memory | `evoflow_memory_sections` | `:860` |
| Memory | `evoflow_person_memory_entries` | `:1047` |
| Memory（员工） | `evoflow_proactive_memory` | `:1169`（与 Agent 域共用） |
| Skills | `evoflow_skills` | `:1264` |
| Skills（任务绑定） | `evoflow_collab_subtask_skills` | `:485` |
| Tools（审批） | `evoflow_tool_approvals` / `_grants` / `_audit` | `:1407` / `:1399` / `:1387` |

### 3.2 无主库表证据的子域

| 子域 | 情况 |
|---|---|
| Tools（执行） | 工具定义不在主库表；`tools.py` 是 **Capability bus**（对外 `/v1` 面），工具实现散在 `evoflow/tools/` 与 `evoflow/community/` |
| MCP（客户端） | 仅 `evoflow_mcp_servers`（`:792`）存服务器配置；**MCP 工具的实际调用不经 SQLite** |

### 3.3 知识库：**不走 SQLite 主库**（本卡最重要的结构发现）

- `rg -n "knowledge" backend/packages/harness/evoflow/persistence/schema.py` → **零命中**。
- 知识库有独立子系统 `backend/packages/harness/evoflow/knowledge/`：
  `chunker.py`、`folders.py`、`index_builder.py`、`llm_indexer.py`、`local_source.py`、`parser.py`、`processor.py`、`service.py`、`wiki_chunker.py`，
  以及子目录 `embedding/`、`owned/`、`vault/`、`vector/`。
- `knowledge/vault/` 是 **vault 文件系统**实现：`builtin.py`、`store.py`、`paths.py`、`provider.py`、`service.py`、`mcp_runtime.py`、`reindex_jobs.py`、`fs_search.py` 等；
  `builtin.py:36-38` 出现 `.obsidian-hybrid-search.db`（含 `-shm` / `-wal`）—— 说明**向量 / 混合检索索引是 vault 目录内的独立数据库文件**，
  不是主库表；`builtin.py:328 resolve_ops_knowledge_vault_path` 是路径解析入口。
- **后果**：知识库的"事实源"是**用户可见的 Markdown 笔记目录**，而非数据库行。
  任何"知识库 → Runtime"的迁移都必须先回答"vault 是否算业务事实源"，这超出本卡授权。

---

## 4. 外部副作用边界（可证明的 3 类）

| # | 副作用 | 证据 | 边界说明 |
|---|---|---|---|
| S1 | **渠道外发**（飞书 / 微信） | `channels.py:305 POST /feishu/push`、`:347/:374/:402` 飞书注册三步、`:470/:494/:523` 微信注册三步；落库 `evoflow_channel_push_log`（`schema.py:315`） | 这是**对外可见**的动作（真实发送到第三方 IM）。注册类端点会与外部平台建立绑定 |
| S2 | **MCP 子进程 / 外部服务** | `mcp.py:125/185 /mcp/config`（配置）、`:248/:269 /mcp/market/search|install`（安装）；配置落 `evoflow_mcp_servers`（`schema.py:792`）；知识库侧另有 `knowledge/vault/mcp_runtime.py` | 安装与启动 MCP 会**拉起外部进程 / 连接外部服务**。本卡未核实安装包来源校验（登记为未知，见 U5） |
| S3 | **工具执行与审批** | `evoflow_tool_approvals` / `_grants` / `_audit`（`schema.py:1407/1399/1387`）；`/api/collab/threads/{thread_id}/tool-approval`（`collab.py:405`） | 工具调用是**有副作用的能力**，本域已有审批闸；但审批与执行之间是否有强制耦合，本卡未证明（登记为未知，见 U6） |

**未证明的外部副作用**：知识库 embedding 是否调用外部模型服务（`knowledge/embedding/` 未展开）、
`/v1/tools/{name}/stream` 的流式副作用语义、渠道凭据的存储与读取路径（§7）。

---

## 5. Runtime 接点：**0**（显式结论）

```
$ rg -n "qagent_runtime|agentscope" \
    backend/app/gateway/routers/{knowledge,knowledge_owned,knowledge_vaults,memory,task_memory,mcp,mcp_server,channels,tools,skills}.py | wc -l
0
```

- 本域**没有**任何 `create_run` / `start_run` / `request_cancel` / 事件回写 / linkage / opt-in 开关。
- 与 Automation 域的差别：Automation 有 3 处既有接线；本域**一处都没有**。
- 卡片要求"分别记录检索、记忆读写、工具 / MCP 调用、渠道发送等真实入口是否已通过既有 Runtime bridge"——
  逐项答案均为 **否**，且**没有任何 bridge 存在**（不是"bridge 存在但未接线"，而是"没有 bridge"）。
- 卡片同时要求"不得假设共享 Adapter、Event、Result 或副作用语义"—— 本卡遵守：§4 的三类副作用各自独立，
  未假设它们共享任何 Runtime 语义。

---

## 6. 权限边界（源码可见）

| 入口 | 鉴权 |
|---|---|
| `/api/knowledge` | `_org_admin_dep`（`knowledge.py:30`） |
| `/v1`（Capability bus） | `verify_capability_bearer_token`（`tools.py:79`），注释明确"app keys use chat/completions"（`tools.py:78`） |
| `/api/channels` | 本卡未在模块头部看到 router 级依赖；逐端点鉴权未展开（登记为未知，见 U7） |
| `/api/knowledge/owned`、`/api/knowledge/vaults`、`/api`（memory / mcp / skills）、`/api/task-detail` | 本卡未核实 router 级依赖 |
| `mcp_server.py`（无前缀） | 本卡未核实 |

---

## 7. 未知项（不得补写成设计）

| # | 未知项 | 说明 |
|---|---|---|
| U1 | 知识库索引的完整存储形态 | 只证明 vault 是文件系统 + `.obsidian-hybrid-search.db`（`builtin.py:36-38`）；索引结构、embedding 维度、重建流程未展开 |
| U2 | 知识库是否调用外部 embedding 服务 | `knowledge/embedding/` 未展开；**不得假设** |
| U3 | `evoflow_memory` / `_facts` / `_sections` / `person_memory_entries` 四表关系 | 本卡只登记存在与行号 |
| U4 | `/v1/tools/{name}/stream` 的流式协议 | 未展开 |
| U5 | MCP 市场安装包的来源校验 | `mcp.py:269` 未核实校验逻辑 |
| U6 | 工具审批与工具执行是否强制耦合 | 审批表存在，但"未审批即拒绝执行"的强制点在源码中未证明 |
| U7 | 渠道与知识库路由的逐端点鉴权 | 只核到 `/api/knowledge` 与 `/v1` 两处 |
| U8 | `evoflow_mcp_servers` 的凭据列与加密方式 | 未展开；**凭据形态不得猜测** |
| U9 | `mcp_server.py`（对外 MCP 服务端）暴露的工具集合 | 未展开 |
| U10 | 五个子域是否应共享一个 Runtime 语义 | 本卡**不做此判断**；卡片明确"不得假设共享 Adapter、Event、Result 或副作用语义" |

---

## 8. A02 是否有证据（结论：**不足以实例化 A02**）

| 判断项 | 结论 |
|---|---|
| 是否有单一业务对象可映射？ | **否**。五个子域（知识 / 记忆 / 工具 / MCP / 渠道）对象不同，卡片本身也要求"分别记录"而非合并 |
| 事实源是否已统一？ | **否**。知识库根本不在主库（§3.3），记忆在 4 张表，MCP 只存配置，工具定义不在库 |
| 外部副作用边界是否已全部证明？ | **否**。§4 只证明 3 类，另有 4 项未展开 |
| 是否满足 roadmap §5.1 的 A03 门槛？ | **否**。Runtime 接点 = 0（§5），且副作用语义未冻结 |

**建议**：本域维持"仅 A01 证据"，`AG-G2-KNOW-002-A01`～`A05` **不实例化**。
若负责人认为有必要，最小可行路径是**先单独决策"知识库 vault 是否算业务事实源"**这一项 ——
它决定了本域是否根本没有可迁移的对象。

## 9. 停止条件检查

| 卡片停止条件 | 本卡是否触发 |
|---|---|
| 没有可证实的真实入口 | **否**。149 个端点全部有 `文件:行` 证据 |
| 需要发明统一 Runtime API / 事件语义 | **否**。未发明；§5 逐项答"否" |
| 需要改底层基础设施 | **否**。未修改任何文件 |
| 发现 G0 决策依赖却无法只读标记 | **否**。本卡未发现需要 G0 决策才能描述的字段级依赖（因接点为 0，尚不存在映射关系）；若将来实例化映射卡，`G0-DEC-001`（审批与执行）与 `G0-DEC-003`（事件协议）会命中 |

## 10. 本卡是否产生代码改动

**否**。只写了 `artifacts/acceptance/agentscope-runtime/AG-G2-KNOW-001-A01/20260915/` 下的
`inventory.md` 与 `acceptance-rg.txt`。未修改任何源代码、测试、schema、migration 或计划外文档；
未创建或删除 branch / worktree；未执行任何被任务包禁止的 git 操作。
