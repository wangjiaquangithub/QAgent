# G2 Chat / Live Run 真实接入地图

> 卡片：AG-G2-CHAT-001-A01（对应母任务 G2-CHAT-001 盘点的第一步）
> 分支：`codex/g2-chat-live-run`　基线：`origin/codex/agentscope-runtime-migration`（`b09adf4`）
> 性质：**只读盘点**。本文件只记录已验证的真实路径，不含任何业务改动。

本文件回答一个最小问题：**现有用户真实使用的「聊天发送 → 执行 → 流式输出 → 前端展示」链路，由哪些真实文件承载。**

---

## 1. 结论速览

| 环节 | 真实承载 | 位置 |
|---|---|---|
| 前端发送入口 | `WsClient.chatSend(sessionKey, message, attachments, contextFiles, opts)` | `evopanel/src/lib/ws-client.js:5472` |
| 前端流式消费 | SSE 读取循环 + `dispatchChatWireSseFrame` | `evopanel/src/lib/ws-client.js:6121`（发起）/`:4614`（分发） |
| 会话/消息 REST 入口 | `chat_sessions` router | `backend/app/gateway/routers/chat_sessions.py`（前缀 `/api/chat/sessions`） |
| 消息写路径（真源） | `chat_svc.append_message_and_touch_session` | `backend/packages/harness/evoflow/persistence/chat_session_service.py` |
| 消息仓储 | `chat_message_repositories` | `backend/packages/harness/evoflow/persistence/chat_message_repositories.py` |
| Live Run 快照 | `live_run_repositories` | `backend/packages/harness/evoflow/persistence/live_run_repositories.py` |
| 执行入口（现状） | LangGraph（in-process mount） | `backend/app/gateway/router_registry.py:103` → `app.mount("/api/langgraph", ...)` |
| 断线续接 | `stream_resume` router + `langgraph_proxy` attach | `backend/app/gateway/routers/stream_resume.py:31`、`langgraph_proxy.py:429` |
| **拟接入 Runtime** | `RuntimeService`（**已挂载**） | `backend/app/qagent_runtime/service.py:33`，HTTP 前缀 `/api/qagent/runtime` |

**关键前提（已验证）**：`app.qagent_runtime.router` **已经在 Gateway 中挂载**（`router_registry.py:88`），无需改动 Gateway 主干即可为 Chat 域提供 Runtime 能力。

---

## 2. 前端真实入口

### 2.1 发送
- 文件：`evopanel/src/lib/ws-client.js`
- 类/方法：`export class WsClient`（`:4713`）→ `async chatSend(...)`（`:5472`）
- 发起请求：`POST /api/langgraph/threads/{threadId}/runs/stream?{ui_sse=1&stream_format=agui}`（`:6121`，路径变量为 `streamPath`）
- 鉴权头：`_authHeaders()`（`:604`）/ `_gatewayAuthFetch`（`:620`）
- 前置步骤：`ensure-thread`（`:5085`）→ `bind-thread`（`:4971`）→ `run-active`（`:5194`）

### 2.2 流式消费与终态收敛
- SSE 解析：`resp.body.getReader()` + `TextDecoder`（`:6123` 起）
- 帧分发：`dispatchChatWireSseFrame(self, opts)`（`:4614`）
- 运行期状态跟踪对象：`evfLane`（`:6128`），含 `finalText` / `finalEmitted` / `evfRunEndSeen` / `streamPath` / `traceId` / `threadId`
- 终态标志：`evfRunEndSeen`、`finalEmitted` —— **终态只应收敛一次**，这是 G2-CHAT-004 必须保持的契约

### 2.3 断线续接（现有 cursor/attach 机制）
- 续读入口：`/api/chat/sessions/{sessionKey}/stream-resume`（`:5289`），实现 `streamResumeFetch`（`evopanel/src/react/lib/stream-resume-fetch.ts`）
- 另有一条 attach 路径：`/api/langgraph/threads/{threadId}/runs/stream`（`:6052`，带 `x-evoflow-stream-resume: 1`）
- LangGraph 不可用降级：`_langGraphDead` 标志（`:475`、`:6099`）

---

## 3. 后端真实入口

### 3.1 `chat_sessions` router
- 文件：`backend/app/gateway/routers/chat_sessions.py`（1791 行）
- 前缀：`/api/chat/sessions`（`:20`）
- 鉴权/归属：`_require_session_access()`（`:45`）→ `evoflow.authz.http_guard.require_session_visible`
- 主体解析：`_request_principal_id()`（`:34`）→ `evoflow.authz.context.resolve_request_principal`

**会话与消息（用户可见真源）**

| 方法 | 路径 | 处理函数 | 行 |
|---|---|---|---|
| GET | `/api/chat/sessions` | `list` | `:423` |
| POST | `/api/chat/sessions` | `create` | `:471` |
| GET | `/{session_key}/messages` | `list messages` | `:883` |
| **POST** | **`/{session_key}/messages`** | **`append_session_message`** | **`:1152`** |
| POST | `/{session_key}/messages/batch` | `append_session_messages_batch` | `:1184` |
| GET | `/by-thread/{thread_id}/messages` | list by thread | `:540` |

**Live Run 相关**

| 方法 | 路径 | 行 | 作用 |
|---|---|---|---|
| GET | `/{session_key}/live-run` | `:958` | 读轻量快照 |
| PUT | `/{session_key}/live-run` | `:979` | 写轻量快照（runId/threadId/status/partialText/…） |
| DELETE | `/{session_key}/live-run` | `:1010` | 清快照 |
| POST | `/{session_key}/heartbeat` | `:1031` | 前端后台保活，刷新 `last_event_at` |
| GET | `/{session_key}/live-run/check` | `:1049` | 判断是否已在后台完成 |
| GET | `/{session_key}/execution/state` | `:1077` | 执行状态聚合（推荐） |
| GET | `/{session_key}/runtime-status` | `:1094` | 已废弃，改用 execution/state |

**执行生命周期**

| 方法 | 路径 | 行 |
|---|---|---|
| POST | `/{session_key}/ensure-thread` | `:1374` |
| POST | `/{session_key}/bind-thread` | `:1457` |
| POST | `/{session_key}/run-active` | `:1476` |
| POST | `/{session_key}/execution/stop` | `:1507` |
| POST | `/{session_key}/run-idle` | `:1535`（已废弃） |

### 3.2 消息写路径（正式真源）
- 主入口：`POST /api/chat/sessions/{session_key}/messages`（`chat_sessions.py:1152`）
- 写入函数：`chat_svc.append_message_and_touch_session(...)`
  - 文件：`backend/packages/harness/evoflow/persistence/chat_session_service.py`
  - 参数：`role` / `content` / `run_id` / `thread_id` / `parent_thread_id` / `principal_id`
  - 可见性校验：`_validate_client_http_append_role`、`_validate_append_body`
- 幂等去重：`messages/batch` 按 `messageId` 去重（`:1184`）
- 计数/读取：`msg_repo.count_messages(key)`、`chat_message_repositories`

### 3.3 执行入口（现状 = LangGraph）
- 挂载点：`backend/app/gateway/router_registry.py:103`
  ```python
  app.mount("/api/langgraph", LazyLangGraphMount(app))
  ```
  另有代理兜底：`langgraph_proxy.router`（`router_registry.py:91`，`include_in_schema=False`）
- 前端实际调用：`POST /api/langgraph/threads/{threadId}/runs/stream`
- 取消：`POST /api/langgraph/runs/cancel`（前端 `ws-client.js:5451`）
- **不得删除**：本卡及后续卡片均不删除 `/api/langgraph`，只在其上做局部、可回退的分流。

### 3.4 SSE / 断线续接
- `backend/app/gateway/routers/stream_resume.py:31` → `GET /{session_key}/stream-resume`（history-poll 续读）
- `backend/app/gateway/routers/langgraph_proxy.py:429` → `GET /threads/{thread_id}/runs/{run_id}/stream`（attach 进行中 run）
- 帧编码助手（共享，**不得改**）：`agui_stream_encode.py`、`agui_stream_normalizer.py`、`openai_stream_encode.py`、`sse_slim.py`、`sse_ui_normalize.py`

---

## 4. 现有状态事实源（真源 vs 投影）

| 状态 | 当前事实源 | 说明 |
|---|---|---|
| 会话列表/上下文 | `evoflow_chat_sessions`（经 `chat_session_service`） | 用户可见真源 |
| 消息历史 | `evoflow_chat_messages`（经 `chat_message_repositories`） | 用户可见真源，`messages/batch` 按 messageId 去重 |
| Lightweight live 快照 | `evoflow_chat_live_runs`（`live_run_repositories`） | **投影/缓存**，可重建，含 `status` / `partialText` / `last_event_at` |
| 执行进度/终态 | 现状由 LangGraph checkpoint + 快照共同决定 | G2-CHAT-004 的目标是让 **Runtime Event 成为执行期事实源**，快照降级为兼容投影 |

**重要**：G2-CHAT-003-A02 必须显式声明「Runtime 侧为执行事实源，`evoflow_chat_live_runs` 仅为兼容投影」，不得把 Runtime 状态再复制成第三套事实源。

---

## 5. 最小拟接入点（下一步改哪里）

### 5.1 Runtime 侧已具备的公开接口（**已验证，无需改 Runtime**）
- 服务类：`RuntimeService`（`backend/app/qagent_runtime/service.py:33`，包导出见 `qagent_runtime/__init__.py`）
- 关键方法：
  - `create_run(*, org_id, task_id, input_payload, idempotency_key=None)`（`:70`）
  - `start_run(run_id, org_id=None)`（`:85`）
  - `stream_events(run_id, *, after_sequence=0, org_id=None)`（`:304`）
  - `request_cancel(run_id, org_id=None)`（`:324`）
  - `resume_run(run_id, org_id=None)`（`:337`）
  - `get_result(run_id, org_id=None)`（`:374`）
  - `grant_approval` / `reject_approval`（`:178` / `:206`）
- HTTP 面（**已挂载**，`prompt="/api/qagent/runtime"`）：
  - `POST /runs`（201）→ `create_run`，`org_id` 取自 `principal.org_id`（**客户端无法伪造组织归属**）
  - `POST /runs/{run_id}/start`
  - `GET /runs/{run_id}/events?after_sequence=N` → SSE，事件带 `id`(= `event_id`) / `event`(= `type`) / `data`
  - `POST /runs/{run_id}/cancel`、`/resume`、`GET /result`、审批 grant/reject
- 鉴权：`get_runtime_principal`（`qagent_runtime/auth.py`）+ `RuntimePrincipal.org_id`

### 5.2 接入位置（Chat 域，最小改动）
- 首选落点：`backend/app/gateway/routers/chat_sessions.py` 与 `backend/packages/harness/evoflow/persistence/chat_session_service.py` 之间的**既有 Chat 服务/适配层**
  - 由 chat 域在已鉴权、已确定 org 作用域之后，调用 `RuntimeService.create_run(...)` / `.start_run(...)`
  - 或经由已挂载的 `/api/qagent/runtime`（服务端内部调用，不新增用户可见 API）
- **不新增**用户可见 API、不新增平行聊天入口、不改前端 URL。
- 开关策略：沿用现有产品既有开关机制；若无可靠开关，仅增加**服务端 opt-in 配置**（不暴露新用户 API），保证关闭时旧链路行为完全不变。

### 5.3 标识映射需求（G2-CHAT-002 的输入）
Runtime 需要的最小标识（`create_run` 契约）：

| Runtime 需要 | 现有可得来源 |
|---|---|
| `org_id` | `RuntimePrincipal.org_id`（服务端解析，客户端不可伪造） |
| 用户主体 | `_request_principal_id(request)` → `resolve_request_principal` |
| Session 标识 | URL 路径 `session_key`（已经 `_require_session_access` 归属校验） |
| 消息标识 | `messageId`（`messages/batch` 已按此去重） |
| Run 标识 | 现状为 LangGraph `run_id`（前端 `run-active` 绑定） |
| 幂等键 | `idempotency_key`（`create_run` 已支持） |

---

## 6. 不得触碰的共享边界

| 边界 | 原因 |
|---|---|
| `backend/app/qagent_runtime/**` | Runtime 内核，本轮只允许**调用**，不允许修改 |
| `backend/migrations/**` | 数据库迁移 |
| `backend/app/gateway/router_registry.py` | Gateway 主干 / router registry |
| `backend/app/gateway/background_startup.py` | 后台启动 |
| `agui_stream_encode.py` / `agui_stream_normalizer.py` / `openai_stream_encode.py` / `openai_stream_normalize.py` / `sse_slim.py` / `sse_ui_normalize.py` | 全局 SSE 基础设施（共享编码层） |
| Automation / Task Center / 无人值守 / App Runner 域 | 非本任务域 |
| 设备协议、lease、fencing | 非本任务域 |
| 前端页面（`evopanel/src/react/**` 页面层） | 保持现有 UI 不变 |
| LangGraph 底层实现 | 只做局部可回退分流，不删除、不重写 |

---

## 7. 下一张卡（AG-G2-CHAT-001-A02）将改的真实入口

对**已验证的真实入口**补最小保护性测试，锁定现有契约（不新建 API、不 mock 整条业务入口）：

- 首要目标：`POST /api/chat/sessions/{session_key}/messages`（`chat_sessions.py:1152`）
  - 断言：核心请求字段（`role` / `content` / `runId` / `threadId`）、会话-消息关联、归属校验（`_require_session_access`）
- 次选目标：`live_run_repositories` 的快照契约（已有 `backend/packages/harness/tests/test_live_run_repositories.py` 可复用其 fixture）
- 现有可复用测试基线：
  - `backend/packages/harness/tests/test_chat_sessions_sqlite.py`
  - `backend/packages/harness/tests/test_chat_message_run_id.py`
  - `backend/packages/harness/tests/test_live_run_repositories.py`
  - `backend/app/tests/test_session_concurrency.py`

---

## 8. 未决问题 / 需在后续卡确认

1. **Chat 是否需要新增 Runtime 公开接口**：就 `create_run` / `start_run` / `stream_events` / `request_cancel` / `get_result` 而言，现有公开面**已足够**。若后续发现缺少「把 chat 消息投影为 Runtime input_payload」或「把 Runtime 终态投影回 message」的官方接口，**必须停止并汇报**，不得改 Runtime 内核。
2. **cursor 语义对齐**：Runtime 事件用 `after_sequence`（整数序列），前端现有 resume 用 `stream-resume` + 历史轮询。G2-CHAT-004 需在 **Chat 域局部 bridge** 内完成映射，不改 Runtime 事件模型。
3. **`org_id` 默认值**：`create_run` 默认 `"local"`。Chat 域必须传入真实已鉴权的 `org_id`，禁止依赖默认值造成跨组织可见。
