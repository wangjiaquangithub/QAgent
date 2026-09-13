# AgentScope 2.0 / 企业运行底座：阶段 0 现状盘点

- 盘点日期：2026-09-13
- 工作目录：`/Users/wangjiaquan/project/QAgent`
- 盘点范围：`/Users/wangjiaquan/project/QAgent/backend/app`、`/Users/wangjiaquan/project/QAgent/backend/packages`，以及 `/Users/wangjiaquan/project/QAgent/backend/langgraph.json`、`/Users/wangjiaquan/project/QAgent/backend/pyproject.toml`
- 目标：为 AgentScope 2.0 替换、PostgreSQL 正式数据收口和云端 / 本地受控执行提供可直接开工的边界、调用链、状态模型和风险清单。
- 约束：本文件只记录现状；本轮没有修改业务代码，没有删除或禁用 LangGraph，也没有执行数据库迁移。

> 本文的“代码已证实”仅表示可以在当前源码的指定文件和符号中定位到实现。没有在受控范围内找到的能力，不等于产品永远不需要，而是必须进入“业务确认”或“运维确认”。

## 0. 执行摘要

当前系统不是单一的 LangGraph 调用，而是四层组合：

```text
渠道 / HTTP API
  → Gateway 路由与 LangGraph 兼容面
  → LangGraph graph / runs.wait / runs.stream
  → SQLite 会话、任务、自动化、协同、审批和资产记录
  → SSE、IM 卡片、附件或任务中心结果
```

同时存在两类运行状态：

1. **框架运行状态**：LangGraph thread/run、checkpointer、`runs.wait` 返回值、`runs.stream` 事件。
2. **QAgent 业务状态**：`evoflow_chat_sessions`、`evoflow_chat_live_runs`、`evoflow_app_runs`、`evoflow_collab_tasks` / `subtasks`、审批表和资产表。

替换的核心难点不是把一个 SDK import 换成另一个 SDK，而是重新定义以下边界：

- QAgent 内部 Runtime 接口：创建运行、输入、事件、结果、停止、恢复、取消、重试。
- AgentScope 状态与 PostgreSQL 业务状态的同步时机和幂等键。
- `/api/langgraph` 的兼容策略、SSE frame/event shape、heartbeat、断线续接和 terminal event。
- 运行中会话、任务中心任务、自动化触发、审批授权和本地执行之间的状态转换。
- 多实例调度的 lease/fencing、恢复和重复触发控制。

**建议阶段 1.5 的最小纵向切片**：`无人值守任务 → Lead 规划 → 授权/审批 → 子任务执行 → 结果/资产元数据回写 → SSE/任务状态收敛`。这条链同时覆盖 Runtime、Run/任务、后台任务、审批、结果和事件恢复，是比单纯聊天更有替换验证价值的真实主链。

---

## 1. LangGraph 引用清单

### 1.1 配置、依赖和运行部署入口

| 优先级 | 文件与符号 | 现状用途 | 入口 / 调用方 | 替换要求 |
|---|---|---|---|---|
| P0 | `/Users/wangjiaquan/project/QAgent/backend/langgraph.json:8-15`，图配置（`lead_agent`、`claude_code_chat`、`goal_agent`） | 声明 LangGraph graph 的模块入口和 `evoflow.agents.checkpointer.async_provider:make_checkpointer` | LangGraph API / 进程内部署加载；Gateway 启动阶段通过 LangGraph API 使用 | AgentScope graph/agent 注册、checkpointer 和运行配置的唯一替代来源；完成替换后删除该部署入口 |
| P0 | `/Users/wangjiaquan/project/QAgent/backend/packages/harness/pyproject.toml:17-20,33-34`，依赖列表 | 直接依赖 `langgraph`、`langgraph-api`、`langgraph-cli`、`langgraph-runtime-inmem`、`langgraph-checkpoint-sqlite`、`langgraph-sdk` | `evoflow-harness` 安装、Gateway import 和运行时 | 不能在迁移初期简单删除；先完成调用面替换和验收，再移除依赖、CLI、服务配置及专属测试 |
| P1 | `/Users/wangjiaquan/project/QAgent/backend/pyproject.toml:21`，`langgraph-sdk` | backend 工作区对 SDK 的直接声明 | 开发/部署环境解析依赖 | 与 harness 依赖保持一致清理，避免“主包已删、workspace 仍可 import”的隐性双轨 |
| P0 | `/Users/wangjiaquan/project/QAgent/backend/app/gateway/app.py:53-217`，lifespan 中的 app-server embed 与 LangGraph shutdown | 桌面模式由 Tauri 以 stdio 子进程承载 app-server；Gateway 可选 embed；退出时停止 LangGraph graph/checkpointer/store/client | `create_app` / lifespan | AgentScope 运行时生命周期和桌面承载方式要分开定义；不能把 desktop stdio 等同于 Agent Runtime |

### 1.2 业务和 Gateway 代码引用

| 优先级 | 文件与符号（源码定位） | 用途 | 入口 / 调用方 | 替换风险与工作项 |
|---|---|---|---|---|
| P0 | `/Users/wangjiaquan/project/QAgent/backend/app/channels/services/goal_service.py:90`：`GoalService.__init__`；`/Users/wangjiaquan/project/QAgent/backend/app/channels/services/goal_service.py:537`：`_sync_session_from_goal_state`；`/Users/wangjiaquan/project/QAgent/backend/app/channels/services/goal_service.py:608`：`_run_goal_graph`；`/Users/wangjiaquan/project/QAgent/backend/app/channels/services/goal_service.py:808`：`ensure_goal_stream_loop`；`/Users/wangjiaquan/project/QAgent/backend/app/channels/services/goal_service.py:1547`：`_persist_session_snapshot`；`/Users/wangjiaquan/project/QAgent/backend/app/channels/services/goal_service.py:1654`：`recover_persisted_sessions`；`/Users/wangjiaquan/project/QAgent/backend/app/channels/services/goal_service.py:1923`：`apply_user_steering`；`/Users/wangjiaquan/project/QAgent/backend/app/channels/services/goal_service.py:1965`：`submit_feedback` | Goal/mission 对话的 thread/run 启动、流式消费、暂停/恢复、用户 steering/feedback，以及把 graph 状态同步到会话快照 | `ChannelManager` 和 Goal API/IM 入口调用 `GoalService`；恢复逻辑由 Gateway 启动触发 | 这是业务语义最厚的 LangGraph 适配层。需要先抽出 Runtime facade，再替换 graph state、stream event 和恢复语义；直接重写容易破坏 goal、反馈和会话一致性 |
| P0 | `/Users/wangjiaquan/project/QAgent/backend/app/channels/manager.py`：`ChannelManager`、`_get_client`、`_handle_chat`、`_handle_streaming_chat`、`cancel_active_im_run`、`_persist_channel_session_index`、`_persist_channel_user_transcript_turn`、`/new` 命令处理 | Feishu/Slack/Telegram/Weixin 等渠道的统一收消息、创建/复用 thread、调用 LangGraph `runs.wait`/`runs.stream`、取消运行、落消息和发送回复/附件 | 渠道 webhook/轮询进入 `ChannelManager`；`GoalService` 也从此处被协调 | 需要保持渠道外部行为不变，同时把 LangGraph-specific client、错误类型、stream payload 解析替换为 QAgent Runtime 事件；`/new` 还涉及会话和 thread 重建 |
| P0 | `/Users/wangjiaquan/project/QAgent/backend/app/gateway/lazy_langgraph.py`：`mount_langgraph_in_process`、`ensure_langgraph_mounted`、`ExternalLangGraphProxy`、`install_external_langgraph_proxy`、`LazyLangGraphMount` | 支持进程内挂载 LangGraph，或通过 `EVOFLOW_LANGGRAPH_URL` 代理外部 LangGraph；延迟挂载以保证 liveness 快速绑定 | `/Users/wangjiaquan/project/QAgent/backend/app/gateway/router_registry.py:50-90` 注册；Gateway lifespan/首次请求触发 | 这层把部署拓扑、HTTP 代理和 Runtime 绑在一起。替换时应明确 AgentScope 是 Gateway 内部服务还是独立 worker；如果保留兼容路由，应只保留 HTTP contract，不保留 LangGraph proxy 实现 |
| P0 | `/Users/wangjiaquan/project/QAgent/backend/app/gateway/router_registry.py:43-107`：`register_core_routers`、`register_gateway_routers` | 注册核心业务路由，并在 `/Users/wangjiaquan/project/QAgent/backend/app/gateway/router_registry.py:89-90` include `/api/langgraph` router；通过 lazy mount 选择进程内或外部 LangGraph | `create_app` / deferred startup | `/api/langgraph` 是当前前端、渠道和脚本可见的框架边界。迁移应先建立稳定的 `/api/runs` 或内部 Runtime contract，再决定 `/api/langgraph` 是兼容别名还是删除 |
| P0 | `/Users/wangjiaquan/project/QAgent/backend/app/gateway/routers/langgraph_proxy.py:22`：`router`；`register_active_stream_proxy`、`unregister_active_stream_proxy`、`reclaim_stale_active_stream_proxies`；`attach_run_stream`（`/Users/wangjiaquan/project/QAgent/backend/app/gateway/routers/langgraph_proxy.py:430-628`）；`get_active_sessions`（`/Users/wangjiaquan/project/QAgent/backend/app/gateway/routers/langgraph_proxy.py:823`） | `/api/langgraph` 反向代理、in-progress run attach/refresh、SSE heartbeat、SSE slim/dedupe、active stream registry、stream mirror、terminal 后会话收敛 | 浏览器/前端的 runs stream 和 active sessions 请求；`run_status_reconcile` 查询活动性 | 替换的 P0 合同：事件类型、帧格式、heartbeat（默认约 15 秒，`EVOFLOW_SSE_HEARTBEAT_INTERVAL`）、断线续接游标、terminal 事件、错误/取消事件、镜像保留策略。不能只替换上游 URL |
| P0 | `/Users/wangjiaquan/project/QAgent/backend/app/gateway/background_startup.py:23-43,89-136,430,890-1146,1153-1231`：`init_startup_state`、`_run_langgraph_selftest`、`run_background_startup`、`run_post_ready_warmups`、`run_automation_scheduler` / `run_task_queue_scheduler` / `run_zombie_sweeper_scheduler` 的启动、`_cancel_scheduler_slot`、`shutdown_gateway_background` | Gateway 启动后异步进行 LangGraph self-test、图 warmup、MCP/知识库 warmup、路由注册；启动 automation/task queue/zombie sweeper 等后台 scheduler；退出时取消调度器并关闭 LangGraph 资源 | `/Users/wangjiaquan/project/QAgent/backend/app/gateway/app.py` lifespan 调用 | 当前“ready”与“Agent Runtime ready”是异步分层的；AgentScope 替换必须定义 readiness、warmup、恢复、失败重试和优雅停机，否则启动后第一条任务可能丢失或卡在 running |
| P0 | `/Users/wangjiaquan/project/QAgent/backend/app/gateway/run_status_reconcile.py:106-648`：`_langgraph_has_active_run`、`discover_active_run_id`、`ensure_session_run_active`、`reconcile_stale_session_runs`、`wait_and_reconcile_on_startup` | 通过 LangGraph run 探测、Gateway active proxy 和 collab phase 修复 SQLite session 的 `idle/running`、`current_run_id`；启动时等待并 reconcile，同时清理过期 stream mirror | `/Users/wangjiaquan/project/QAgent/backend/app/gateway/background_startup.py` 启动 hook；session/run API 和 stream attach 间接依赖 | 这是“框架状态反推业务状态”的关键债务。替换后应让 QAgent Run 记录成为状态来源，AgentScope 只提供执行观测；需设计 run lease、心跳、terminal 幂等和重启恢复 |
| P0 | `/Users/wangjiaquan/project/QAgent/backend/app/gateway/automation_runner.py:42-210,590-707,766-827,1087-1140,1298-1520`：`_langgraph_run_semaphore`、`_task_wants_langgraph_run`、`_invoke_langgraph_for_automation`、`_enqueue_automation_as_unattended_task`、`_kick_unattended_task`、`_run_bound_app_for_automation`、`_run_one_task`、`_run_one_task_direct_langgraph`、`automation_tick`、`run_automation_scheduler`、`automation_scheduler_enabled` | 自动化到期扫描；绑定 App workflow、prompt-only direct LangGraph、Task Center/unattended 三条执行分支；写 automation run、投递结果/通知 | `/Users/wangjiaquan/project/QAgent/backend/app/gateway/background_startup.py:993-1005` 创建 scheduler；配置默认约 60 秒 tick，`EVOFLOW_AUTOMATION_SCHEDULER=0` 可关闭；LangGraph 并发由 `EVOFLOW_AUTOMATION_LANGGRAPH_MAX_CONCURRENT` 控制，默认约 2 | 调度、幂等、并发和重试没有统一的分布式 lease/fencing。AgentScope worker 化后容易发生多实例重复触发；必须先确定 trigger id、claim、heartbeat、cancel 和重试边界 |
| P0 | `/Users/wangjiaquan/project/QAgent/backend/app/gateway/unattended_task_pipeline.py:239-334,398-457,599-834,841-917`：`_prepare_unattended_plan_context`、`_build_unattended_plan_prompt`、`_trigger_unattended_plan_run`、`_ensure_langgraph_thread`、`_finalize_executing_task`、`_advance_unattended_task_impl`、`advance_unattended_task`、candidate/in-progress 查询 | 无人值守主任务的 Lead plan → authorize → dispatch → execute → finalize 生命周期；为规划创建 LangGraph thread/run，并推进协同任务/子任务 | `automation_runner._enqueue_automation_as_unattended_task` / `_kick_unattended_task`；task queue scheduler 也会推进 | 是推荐的阶段 1.5 主链。替换重点是 planning run 与 task record 的绑定、子任务幂等、授权后才能执行、失败/超时/取消/重试和结果回写 |

### 1.3 LangGraph 引用的替换顺序

1. **先抽象（P0）**：建立 QAgent 内部 Runtime facade，至少覆盖 `create_run`、`send_input`、`stream_events`、`wait`、`cancel`、`resume/attach`、`get_run`、`get_state`；业务代码不再直接依赖 `langgraph_sdk` 类型或 `runs.wait` 结果形状。
2. **先切真实主链（P0）**：无人值守 planning/execution 走 facade，再接 AgentScope；保留旧 LangGraph 作为验收对照，不删除。
3. **迁移交互层（P0）**：`GoalService`、`ChannelManager`、`langgraph_proxy` 的 SSE contract、stream mirror 和 active session 语义统一到 facade。
4. **迁移后台层（P0）**：automation、task queue、startup/reconcile/zombie sweeper 统一使用 Run 状态和 lease。
5. **最后清理（P1）**：删除 `langgraph.json`、LangGraph proxy/deployment、SDK/依赖和仅服务 LangGraph 的测试；清理旧字段只能在数据迁移验收后进行。

---

## 2. SQLite 数据实体清单

### 2.1 存储边界和现状

- `/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/config/storage_config.py:10-24` 的 `StorageConfig.backend` 当前只有 `Literal["sqlite"]`；默认应用库是 `/Users/wangjiaquan/project/QAgent/backend/data/app/evoflow.db`，LangGraph checkpoint 通常独立在 `/Users/wangjiaquan/project/QAgent/backend/data/checkpoints/checkpoints.db`。
- `/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/db.py:155-190`：`resolve_evolflow_db_path` 解析数据库路径。
- `/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/db.py:227-333`：`run_db_transaction`、`_open_shared_connection`，通过单进程共享连接、进程锁、WAL、busy timeout 运行。
- `/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/db.py:353-490`：`run_db_with_retry`、`run_db_read`、`get_db`；写入带重试，读取有只读连接池。
- `/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/db.py:717-760`：`run_deferred_full_preflight`，启动后做 integrity/quick check 和 WAL checkpoint。
- `/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/schema.py:1-16`：公开 `APP_SCHEMA_VERSION = 1`，历史 `user_version 1..142` 已压缩为一个 schema epoch；不能只看 `PRAGMA user_version` 判断物理 schema 是否完整。
- `/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/schema.py:2157-2172`：`ensure_app_schema` 对现有库做 baseline / legacy 兼容补齐，包括旧 authz 和 chat sessions 字段。

### 2.2 业务实体、字段和读写入口

下表列出运行底座相关表及关键字段，不复制完整 DDL；字段以 `/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/schema.py` 当前 baseline 为准。

| 领域 | 表与关键字段 | 主要读写入口 | 迁移风险 |
|---|---|---|---|
| App 定义 | `evoflow_apps`：`id`、`name`、`parameters_json`、`steps_json`、`goal_template`、`execution_mode`、`auto_run`、`source_task_id`、`version`、`status`、`org_id`、`owner_scope_id`、`created_by`（`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/schema.py:223-245`）；`evoflow_app_revisions`：`app_id`、`version`、`snapshot_json`、`status`、`note`、`created_at`（`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/schema.py:187-196`） | `/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/collab/app_runner.py`：`run_app_workflow`（`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/collab/app_runner.py:639`）、`run_app`（`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/collab/app_runner.py:843`）、`get_run_status`（`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/collab/app_runner.py:920`）、`cancel_run`（`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/collab/app_runner.py:1146`）、`pause_run`（`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/collab/app_runner.py:1207`） / `resume_run`（`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/collab/app_runner.py:1241`）；Gateway app/task routers | JSON steps、canvas、模板和版本字段不能在迁移中降级；`app_runs` 与 collab task/thread 的外键语义需要统一 |
| App Run | `evoflow_app_runs`：`id`、`app_id`、`app_version`、`parameters_json`、`execution_mode`、`task_id`、`thread_id`、`status`、`progress`、`result_summary`、`created_at`、`started_at`、`completed_at`、`error`（`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/schema.py:198-214`） | 同上 `/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/collab/app_runner.py` 的 run/cancel/pause/resume；`/Users/wangjiaquan/project/QAgent/backend/app/gateway/automation_runner.py` 的 bound app 分支 | 现有 `status` 是文本自由值，且 `task_id`/`thread_id` 可为空/非外键；迁移需定义状态枚举、幂等键和 run 与任务的 1:1/1:N 关系 |
| 会话 | `evoflow_chat_sessions`：`session_key`、`thread_id`、`session_status`、`run_status`、`current_run_id`、`collab_task_id`、`org_id`、`scope_id`、`created_by`，以及 context、workspace、model、plan、active/pending tools、token 字段（`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/schema.py:386-411`） | `/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/session_run_state.py`；`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/chat_session_service.py`；`/Users/wangjiaquan/project/QAgent/backend/app/channels/manager.py`；`/Users/wangjiaquan/project/QAgent/backend/app/channels/services/goal_service.py`；chat/session routers | `session_status` 与 `run_status` 双状态可能漂移；`thread_id` 是框架耦合键；切换多设备/云端后需重新定义会话归属和可见性 |
| 当前 Live Run | `evoflow_chat_live_runs`：`session_key`、`thread_id`、`run_id`、`status`、`partial_text`、`partial_tools_json`、`last_event_at`、`display_segments_json`（`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/schema.py:339-348`） | `/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/live_run_repositories.py`、`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/session_run_state.py`；`/Users/wangjiaquan/project/QAgent/backend/app/channels/services/goal_service.py`；Gateway streaming | 仅单行/会话主键模型，不适合天然多并发 run；要决定一个 session 是否允许多个 run、active run 唯一约束和断线恢复点 |
| Transcript / 注入 | `evoflow_chat_messages`：`session_key`、`seq`、`role`、`content_json`、`tool_call_id`、`tool_name`、`run_id`、`thread_id`、token 字段、`round_id`（`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/schema.py:350-369`）；`evoflow_chat_pending_inject`：消息/工具注入与 `run_id`、`thread_id`、`consumed_at`、`consumed_by_run_id`（`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/schema.py:371-384`） | `/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/chat_message_repositories.py`、`transcript_run_id.py`、`transcript_resume_anchor.py`；`ChannelManager`、`GoalService` | `seq` 是 `(session_key, seq)` 唯一；JSON content、工具调用和 token 需保留；注入消费必须幂等，否则重连或重试会重复执行 |
| SSE 流镜像 | `evoflow_chat_stream_mirror`：`session_key`、`thread_id`、`run_id`、`seq`、`raw_frame`、`is_terminal`，唯一键 `(session_key, run_id, seq)`（`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/schema.py:424-434`）；`evoflow_chat_stream_mirror_meta`：frame/byte count、last seq、过期时间（`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/schema.py:436-445`） | `/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/stream_mirror_repositories.py`；`/Users/wangjiaquan/project/QAgent/backend/app/gateway/routers/langgraph_proxy.py`；`streaming/stream_mirror_background.py`；`stream_resume` router | 当前保存的是 LangGraph/SSE raw frame；AgentScope event schema 变化会影响重放、去重、终态判断和空间占用；大 payload 还会放大 SQLite WAL/迁移窗口 |
| 自动化定义 | `evoflow_automations`：`task_id`、`prompt`、`schedule`/`rrule`、`status`、`schedule_type`、workspace、有效期、`langgraph_run`、`langgraph_thread_mode`、`langgraph_thread_id`、timeout、`once_fired`（`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/schema.py:273-304`） | `/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/automation_repositories.py`；`/Users/wangjiaquan/project/QAgent/backend/app/gateway/automation_runner.py` 的 `_effective_cron`、`automation_tick`、`load_automation_toml`；automation routers | 表名仍含 legacy 字段，计划表达式和“只执行一次”语义迁移需保留；TOML 与 SQLite 双来源要先定 SSOT |
| 自动化 Run | `evoflow_automation_runs`：`id`、`task_id`、`run_id`、`status`、`output`、`error`、duration、`langgraph_thread_id`、`langgraph_run`、`extra_json`、timestamps（`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/schema.py:257-271`） | `automation_repositories.py`；`automation_runner._run_one_task`、`_invoke_langgraph_for_automation`、`_run_one_task_direct_langgraph` | `run_id` 可空、状态自由文本、输出大字段；需增加 trigger/claim/idempotency/worker/attempt 信息或建立等价新表 |
| 协同主任务 | `evoflow_collab_tasks`：`main_task_id`、`task_id`、`status`、`progress`、`execution_authorized`、`thread_id`、`authorized_at`、`authorized_by`、`plan_*`、`result_json`、`org_id`、`owner_scope_id`、`created_by`（`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/schema.py:553-574`） | `/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/task_repositories.py`；`/Users/wangjiaquan/project/QAgent/backend/app/gateway/unattended_task_pipeline.py`；task routers | `main_task_id + task_id` 主键、状态值多且跨主/子任务解释不同；规划和执行共用记录，需拆出或明确状态机 |
| 协同子任务 | `evoflow_collab_subtasks`：`main_task_id`、`parent_task_id`、`subtask_id`、`status`、`assigned_to`、`execution_authorized`、`thread_id`、时间、`result_json`、worker 配置、`worker_max_retries`（`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/schema.py:503-534`） | `/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/task_repositories.py`、`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/collab/storage.py`、`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/collab/plan_subtasks_sync.py`、`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/collab/task_state_service.py`；`/Users/wangjiaquan/project/QAgent/backend/app/gateway/unattended_task_pipeline.py`；task queue runner | 依赖 DAG、授权、worker session、重试和结果混在一张表；并发 dispatch 时必须有 claim/version 条件更新 |
| 协同关系/历史 | `evoflow_collab_task_deps`、`evoflow_collab_subtask_deps`、`evoflow_collab_task_execution_history`、`evoflow_collab_peer_messages`、`evoflow_collab_subtask_expected_outputs`、`evoflow_collab_subtask_skills`、`evoflow_collab_subtask_tools`；字段定义分别见：`evoflow_collab_peer_messages`（`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/schema.py:447-464`）、`evoflow_collab_subtask_deps`（`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/schema.py:466-474`）、`evoflow_collab_subtask_expected_outputs`（`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/schema.py:476-484`）、`evoflow_collab_subtask_skills`（`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/schema.py:485-493`）、`evoflow_collab_subtask_tools`（`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/schema.py:494-502`）、`evoflow_collab_task_deps`（`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/schema.py:536-542`）、`evoflow_collab_task_execution_history`（`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/schema.py:544-551`） | `/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/task_repositories.py`、`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/collab/storage.py`、`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/collab/plan_subtasks_sync.py` | DAG 和事件历史迁移到 PostgreSQL 时要保持排序、重复保护和回放顺序；历史 JSON 不能只转为日志 |
| Goal / Mission | `evoflow_goal_sessions`（`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/schema.py:676-714`）；`evoflow_mission_nodes`、`evoflow_mission_retries`、`evoflow_mission_runtime`、`evoflow_mission_state`、`evoflow_mission_state_items`、`evoflow_mission_state_scenarios`、`evoflow_mission_subproblems`（`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/schema.py:869-950`） | `/Users/wangjiaquan/project/QAgent/backend/app/channels/services/goal_service.py`；`mission_node_repositories.py`、`mission_state_repositories.py`；`/Users/wangjiaquan/project/QAgent/backend/app/gateway/routers/goal.py` 及 hosted goal routers | Goal 的 objective/intent/plan/turn/drift/retry 状态既有结构化字段又有 JSON/Markdown；要确认哪些是正式业务状态，哪些只是可重建推理缓存 |
| 文件资产 | `evoflow_artifacts`：`session_key`、`thread_id`、`artifact_id`、`path`、`name`、`url`、`mime`、`size`、`content`、`status`、`meta_json`、timestamps（`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/schema.py:247-255`）；`evoflow_org_artifacts`：`org_instance_id`、`artifact_type`、`artifact_id`、`created_at`（`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/schema.py:986-993`） | `/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/artifact_repositories.py`；`/Users/wangjiaquan/project/QAgent/backend/app/gateway/routers/artifacts.py`、`/Users/wangjiaquan/project/QAgent/backend/app/channels/manager.py` 的 `_prepare_artifact_delivery` 等；组织 registry | `content` 可能把原件放进 SQLite；企业正式库计划只保存元数据时，必须先盘点实际使用和文件原件位置，避免迁移后链接失效 |
| 媒体资产 | `evoflow_media_assets`：`id`、`thread_id`、`tool_name`、`media_kind`、`provider`、`task_id`、`status`、`remote_url`、`local_path`、`file_size_bytes`、`meta_json`、timestamps（`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/schema.py:807-823`） | media asset repositories、media tool handlers、`media_assets` router、ChannelManager 附件发送 | `local_path`/`remote_url` 是部署拓扑耦合；云端与本地执行分离后要增加来源设备、可访问性、同步回执和权限语义 |
| 审批 | `evoflow_proactive_approvals`：`id`、`initiative_id`、`role_agent_code`、`channel`、Feishu message、`status`、decider/time/comment、escalation、`task_id`（`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/schema.py:1117-1131`）；`evoflow_tool_approvals`、`evoflow_tool_approval_grants`、`evoflow_tool_approval_audit`（`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/schema.py:1387-1420`） | `/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/proactive/repositories.py`、`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/tool_approval_repositories.py`；`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/proactive/decision_gate.py`；approval settings/API/channel handlers | 主动任务审批与工具调用审批是两套对象，不能在迁移时合并成一个 `approval_status`；需保留审计、签名/授权 grant、超时和外部消息回调关联 |

### 2.3 数据量获取方式

当前盘点没有把任何本地数据库样本行数当作事实。迁移前应在**停写窗口或一致性快照**上执行以下 SQL（数据库路径由 `resolve_evolflow_db_path` 决定）：

```sql
-- 表清单
SELECT name
FROM sqlite_master
WHERE type = 'table' AND name LIKE 'evoflow_%'
ORDER BY name;

-- 关键表行数
SELECT 'evoflow_chat_sessions', COUNT(*) FROM evoflow_chat_sessions
UNION ALL SELECT 'evoflow_chat_messages', COUNT(*) FROM evoflow_chat_messages
UNION ALL SELECT 'evoflow_chat_stream_mirror', COUNT(*) FROM evoflow_chat_stream_mirror
UNION ALL SELECT 'evoflow_collab_tasks', COUNT(*) FROM evoflow_collab_tasks
UNION ALL SELECT 'evoflow_collab_subtasks', COUNT(*) FROM evoflow_collab_subtasks
UNION ALL SELECT 'evoflow_app_runs', COUNT(*) FROM evoflow_app_runs
UNION ALL SELECT 'evoflow_automation_runs', COUNT(*) FROM evoflow_automation_runs
UNION ALL SELECT 'evoflow_artifacts', COUNT(*) FROM evoflow_artifacts
UNION ALL SELECT 'evoflow_media_assets', COUNT(*) FROM evoflow_media_assets;

-- 时间范围（各表按实际时间字段执行）
SELECT MIN(created_at), MAX(created_at) FROM evoflow_chat_messages;
SELECT MIN(created_at), MAX(created_at) FROM evoflow_collab_tasks;
SELECT MIN(created_at), MAX(created_at) FROM evoflow_app_runs;

-- SQLite 物理文件（数据库外执行）
-- /Users/wangjiaquan/project/QAgent/backend/data/app/evoflow.db、/Users/wangjiaquan/project/QAgent/backend/data/app/evoflow.db-wal、/Users/wangjiaquan/project/QAgent/backend/data/app/evoflow.db-shm
```

还需要按大字段测量字节数：`content_json`、`raw_frame`、`output`、`result_json`、`payload_json`、`meta_json`；并记录 `PRAGMA page_count`、`page_size`、`freelist_count`、WAL 文件大小。PostgreSQL 目标侧应建立每表 `pg_total_relation_size`、索引大小和预计增长率基线。

### 2.4 SQLite → PostgreSQL 的主要迁移风险

- **单进程假设**：`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/db.py` 的共享连接、进程锁和 WAL 能缓解本机并发，但不能替代 PostgreSQL 的多实例事务、行锁、租约和故障转移。
- **时间类型不统一**：schema 同时使用 TEXT、INTEGER、REAL 和 SQLite 默认 `strftime`；迁移时必须统一时区、精度和 null 语义。
- **状态值未集中枚举**：Run、协同任务、子任务、审批和资产状态多为 TEXT，自由值会导致跨服务状态机不一致。
- **JSON 大字段**：SQLite TEXT 中的 JSON 既是业务载荷又是事件/快照；需要决定 PostgreSQL `jsonb`、压缩/归档和索引策略。
- **弱外键 / legacy 字段**：部分 `thread_id`、`task_id` 没有完整外键约束，历史数据可能存在孤儿记录。
- **schema epoch 压缩**：`APP_SCHEMA_VERSION=1` 不能证明所有安装的物理列完整；迁移前必须按表/列/索引校验，不得只读 `user_version`。
- **文件与数据库混存**：`artifacts.content`、`media_assets.local_path`、`remote_url` 可能依赖单机文件；正式库收口只能迁移可访问的元数据和明确的原件引用。
- **LangGraph checkpoint 分库**：`/Users/wangjiaquan/project/QAgent/backend/data/checkpoints/checkpoints.db` 与应用库不是同一迁移对象；必须明确 checkpoint 是否只做过渡恢复数据，不能把它当企业正式状态。

---

## 3. API、SSE、后台任务、自动化和无人值守调用链

### 3.1 HTTP API / Gateway 注册链

```text
FastAPI create_app / lifespan
  → /Users/wangjiaquan/project/QAgent/backend/app/gateway/router_registry.py
      register_core_routers / register_gateway_routers
  → include 业务 routers（threads、tasks、runs、goal、artifacts、stream_resume 等）
  → include app.gateway.routers.langgraph_proxy.router
      prefix=/api/langgraph
  → `/Users/wangjiaquan/project/QAgent/backend/app/gateway/lazy_langgraph.py`
      进程内 mount 或 ExternalLangGraphProxy 外部转发
```

源码依据：

- `/Users/wangjiaquan/project/QAgent/backend/app/gateway/router_registry.py:43-90`。
- `/Users/wangjiaquan/project/QAgent/backend/app/gateway/lazy_langgraph.py:40-320`。
- `/Users/wangjiaquan/project/QAgent/backend/app/gateway/app.py:220-250` 的 `create_app`。

### 3.2 浏览器 SSE / 断线续接链

```text
前端请求 /api/langgraph/.../runs/stream
  → Gateway LangGraph mount/proxy
  → LangGraph graph runs.stream
  → proxy 做 SSE slim/dedupe + reasoning 记录
  → register_active_stream_proxy(thread_id, run_id)
  → stream mirror repositories 写 raw frame / seq / terminal
  → StreamingResponse(text/event-stream)
  → 浏览器断线后 GET /api/langgraph/threads/{thread_id}/runs/{run_id}/stream
  → attach_run_stream 重放/镜像并继续上游
  → terminal event
  → force_end_session_turn / session_run_state 清理 current_run_id、run_status
```

源码依据：

- `/Users/wangjiaquan/project/QAgent/backend/app/gateway/routers/langgraph_proxy.py:22-55,59-108,267-308,429-627,822-863`。
- `/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/stream_mirror_repositories.py`：镜像写入、读取、过期清理。
- `/Users/wangjiaquan/project/QAgent/backend/app/gateway/routers/stream_resume.py`：会话流恢复 API（入口由 `/Users/wangjiaquan/project/QAgent/backend/app/gateway/router_registry.py:72` 注册）。
- `/Users/wangjiaquan/project/QAgent/backend/app/gateway/run_status_reconcile.py:563-645`：终态和启动恢复。

当前可见的 SSE 约束：heartbeat 默认约 15 秒（`EVOFLOW_SSE_HEARTBEAT_INTERVAL`，最小 5 秒）；还存在 `EVOFLOW_SSE_SLIM`、`EVOFLOW_SSE_SLIM_DEDUPE_VALUES`、`EVOFLOW_UI_SSE` 等兼容开关。AgentScope 事件不得直接暴露为未经版本化的内部对象。

### 3.3 渠道消息链

```text
Feishu / Slack / Telegram / Weixin webhook 或轮询
  → ChannelManager._handle_chat / _handle_streaming_chat
  → _get_client 创建 LangGraph SDK client
  → threads/runs.wait 或 threads/runs.stream
  → 解析 assistant/custom/tool 事件
  → _persist_channel_user_transcript_turn + session index
  → 发送文本、卡片、artifact/附件
  → cancel_active_im_run 或 /new 触发取消/新 thread
```

源码依据：`/Users/wangjiaquan/project/QAgent/backend/app/channels/manager.py:15-30,393-539,577-818` 及文件内 `ChannelManager`、`_get_client`、`_handle_chat`、`_handle_streaming_chat`、`cancel_active_im_run`。

### 3.4 Gateway 启动、后台任务和关闭链

```text
`/Users/wangjiaquan/project/QAgent/backend/app/gateway/app.py` lifespan
  → init_startup_state
  → background_startup.run_background_startup
      → LangGraph self-test / mount / graph warmup
      → channels、MCP、知识库等 warmup
      → run_automation_scheduler（可配置）
      → run_task_queue_scheduler（可配置）
      → run_zombie_sweeper_scheduler（可配置）
      → wait_and_reconcile_on_startup
  → 服务进入 ready，但部分重任务仍在后台
  → shutdown_gateway_background
      → stop scheduler tasks
      → stop LangGraph/checkpointer/store/client（非 external mode）
```

源码依据：`/Users/wangjiaquan/project/QAgent/backend/app/gateway/background_startup.py:89-136,430,890-1146,1153-1231` 的 `_run_langgraph_selftest`、`run_background_startup`、`run_post_ready_warmups`、`_cancel_scheduler_slot`、`shutdown_gateway_background`；`/Users/wangjiaquan/project/QAgent/backend/app/gateway/app.py:176-217` 的 lifespan。

### 3.5 自动化任务链

```text
automation_tick
  → 查询到期 evoflow_automations / legacy TOML
  → claim/判断 once_fired、schedule、有效期和并发槽
  → 分支 A：绑定 app_id
        → app_runner.run_app / run_app_workflow
        → render plan → 子任务 DAG → app_run / task 状态
  → 分支 B：prompt-only
        → _invoke_langgraph_for_automation / _run_one_task_direct_langgraph
        → runs.wait → evoflow_automation_runs output/error
  → 分支 C：execution_mode=task_center 或 EVOFLOW_AUTOMATION_VIA_TASK=1
        → _enqueue_automation_as_unattended_task
        → _kick_unattended_task → task queue / unattended pipeline
  → 通知、结果和下次调度时间
```

源码依据：`/Users/wangjiaquan/project/QAgent/backend/app/gateway/automation_runner.py:210-440,590-707,766-827,1087-1140,1298-1520`；`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/collab/app_runner.py`。默认 tick 约 60 秒；LangGraph 自动化并发默认约 2。

### 3.6 无人值守任务链

```text
自动化 / Task Center 创建主任务
  → evoflow_collab_tasks(status=pending, run_mode=unattended,
    unattended_stage=queued, execution_authorized=True, authorized_by=automation)
  → _prepare_unattended_plan_context
  → _build_unattended_plan_prompt
  → _ensure_langgraph_thread / _trigger_unattended_plan_run
  → Lead 生成计划并落 plan_*、子任务和依赖 DAG
  → authorize / approval
  → dispatch eligible subtasks
  → local/cloud worker execution
  → _finalize_executing_task
  → 主任务 completed / failed / cancelled
  → result_json、artifact metadata、SSE/task status 收敛
```

源码依据：`/Users/wangjiaquan/project/QAgent/backend/app/gateway/unattended_task_pipeline.py:239-334,398-457,599-917`；`/Users/wangjiaquan/project/QAgent/backend/app/gateway/automation_runner.py:590-707`。实际代码同时出现 `pending`、`planning`、`planned`、`waiting_dispatch`、`executing`、`running`、`in_progress` 等活跃值，以及 `completed`、`done`、`success`、`failed`、`error`、`cancelled`、`canceled`、`timed_out`、`skipped` 等终态值；这应在阶段 1 先收敛为显式状态机，而不是在迁移时逐字搬运。

---

## 4. 当前状态模型

### 4.1 Run 状态

当前不存在一个完全统一的 Run 表；至少有以下相互关联的表示：

```text
LangGraph run_id / thread_id
  ↕（当前由适配层和探测逻辑同步）
evoflow_chat_sessions.current_run_id + run_status
  ↕
evoflow_chat_live_runs.run_id + status + partial snapshot
  ↕
evoflow_chat_stream_mirror.run_id + seq + is_terminal
  ↕
evoflow_app_runs.id + status/progress/result/error
  ↕
evoflow_automation_runs.run_id + status/output/error
```

代码已证实的运行状态事实：

- 会话默认 `run_status='idle'`，运行中通过 `current_run_id` 和 live run 记录关联：`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/schema.py:386-411`、`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/live_run_repositories.py`、`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/session_run_state.py`。
- Gateway active stream registry 是进程内 best-effort 记录：`/Users/wangjiaquan/project/QAgent/backend/app/gateway/routers/langgraph_proxy.py:52-108`；不能作为多实例正式状态。
- `/Users/wangjiaquan/project/QAgent/backend/app/gateway/run_status_reconcile.py` 通过探测 LangGraph run、active proxy 和协同 phase 反推会话是否仍在运行：`/Users/wangjiaquan/project/QAgent/backend/app/gateway/run_status_reconcile.py:106-220,563-645`。
- App Run 具备 `running` 默认值、progress、started/completed/error，但与 LangGraph run 不存在统一强外键：`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/schema.py:198-214`。

阶段 1 应定义统一 Run 状态机，建议至少区分：`created`、`queued`、`planning`、`running`、`waiting_approval`、`paused`、`cancelling`、`completed`、`failed`、`cancelled`、`timed_out`；每次状态变更带 `run_id`、版本/条件更新、actor、原因、时间和事件序号。

### 4.2 任务和子任务状态

**主任务**：`evoflow_collab_tasks`（`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/schema.py:553-574`）保存 `status`、`progress`、`execution_authorized`、`thread_id`、计划字段、`result_json`、错误和归属。无人值守初始语义由 `/Users/wangjiaquan/project/QAgent/backend/app/gateway/unattended_task_pipeline.py` 设为 `pending / queued`，并绑定自动化授权；终态为 completed/failed/cancelled。

**子任务**：`evoflow_collab_subtasks`（`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/schema.py:503-534`）保存父子关系、assigned worker、授权、thread/session、worker 指令、最大重试和 `result_json`。依赖表和执行历史：`evoflow_collab_peer_messages`（`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/schema.py:447-464`）；`evoflow_collab_subtask_deps`（`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/schema.py:466-474`）；`evoflow_collab_subtask_expected_outputs`（`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/schema.py:476-484`）；`evoflow_collab_subtask_skills`（`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/schema.py:485-493`）；`evoflow_collab_subtask_tools`（`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/schema.py:494-502`）；`evoflow_collab_task_deps`（`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/schema.py:536-542`）；`evoflow_collab_task_execution_history`（`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/schema.py:544-551`）。

当前风险是：计划阶段、调度阶段、执行阶段、验证阶段使用同一组文本状态；同一语义存在多个拼写（`cancelled/canceled`、`completed/done/success`）。PostgreSQL 化和 AgentScope 替换应同步做 canonical enum + compatibility mapping。

### 4.3 审批状态

审批至少分为两条链：

1. **主动任务 / initiative 审批**：`evoflow_proactive_approvals.status` 默认 `pending`，带 `decided_by`、`decided_at`、`decision_comment`、`escalation_level`、`task_id`；见 `/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/schema.py:1117-1131`。
2. **工具调用审批**：`evoflow_tool_approvals.status` 默认 `pending`，带 `tool_call_id`、`tool_name`、`args_json`、`signature`；`evoflow_tool_approval_grants` 保存 session 级 grant，`evoflow_tool_approval_audit` 保存动作审计；见 `/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/schema.py:1387-1420`。

`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/proactive/decision_gate.py` 负责决策门、桌面/飞书推送和回调协调；审批不能只被建模成 AgentScope 的暂停点，必须有 QAgent 正式记录和审计事件。

### 4.4 设备命令状态

**当前源码尚未证实一个统一的设备命令实体、命令表、command handler 或云端到设备投递协议。** 已证实的是：

- 桌面 Tauri 以 stdio 子进程运行 app-server：`/Users/wangjiaquan/project/QAgent/backend/app/gateway/app.py:182-199`。
- stdio 编码/进程承载处理在 `/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/desktop_stdio.py`。
- 本地命令/进程工具位于 `/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/tools/builtins/`，包括 `execute_command`、bash/进程执行相关实现。
- 审批和通知门位于 `/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/proactive/decision_gate.py`。

因此当前只能画出：

```text
Agent/工具调用
  → tool approval（如命中策略）
  → 本地 built-in command/process tool
  → app-server / desktop stdio 承载
  → 本机文件、进程、桌面环境
```

还不能从当前盘点得出：设备在线/离线状态、命令唯一 ID、送达/执行/回执/超时/取消状态、设备身份与凭据、云端重试和 fencing。若设备命令是 P0，阶段 1.5 必须增加最小幂等命令 envelope 和回执表；否则将其列为后续本地执行切片，不要在 Runtime 替换中隐式发明协议。

### 4.5 资产状态

- `evoflow_artifacts.status` 默认 `new`，记录会话/thread 下的 artifact、路径/URL/MIME/大小/content/meta：`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/schema.py:247-255`。
- `evoflow_media_assets.status` 与 `provider`、`remote_url`、`local_path`、`task_id` 一起表达媒体生成/落盘/远端引用：`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/schema.py:807-823`。
- `evoflow_org_artifacts` 只表达组织实例与 artifact 的归属/引用关系：`/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/persistence/schema.py:986-993`。
- 渠道侧会在 `ChannelManager` 的 artifact delivery 逻辑中提取并发送结果；Gateway 有 artifacts/media_assets routers。

建议未来规范化为：`declared → producing → available → synced → failed → deleted`，并把“原件存储位置”和“数据库元数据状态”分开；当前 `new` 等值只能视为现状兼容值。

---

## 5. 阶段 1.5 推荐的最小纵向切片

### 5.1 选择的真实业务主链

```text
创建无人值守任务
  → Lead planning run
  → 计划持久化和审批/授权
  → 选择可执行子任务并 dispatch
  → AgentScope worker 执行
  → 子任务结果与资产元数据回写
  → 主任务 finalize
  → SSE / Task Center 查询看到一致终态
  → 重启或断线后可恢复/重放
```

具体落点：

1. 创建与入队：`/Users/wangjiaquan/project/QAgent/backend/app/gateway/automation_runner.py:_enqueue_automation_as_unattended_task`，写 `evoflow_collab_tasks`。
2. 规划：`/Users/wangjiaquan/project/QAgent/backend/app/gateway/unattended_task_pipeline.py:_prepare_unattended_plan_context`、`_build_unattended_plan_prompt`、`_trigger_unattended_plan_run`、`_ensure_langgraph_thread`。
3. 授权：`/Users/wangjiaquan/project/QAgent/backend/app/gateway/unattended_task_pipeline.py:_advance_unattended_task_impl` 配合 `/Users/wangjiaquan/project/QAgent/backend/packages/harness/evoflow/proactive/decision_gate.py` 和审批 repositories。
4. 执行：`evoflow_collab_subtasks`、task queue runner、worker/local tool；替换点是 AgentScope Runtime facade。
5. 回写：`_finalize_executing_task`、`result_json`、artifact/media repositories。
6. 对外观察：`langgraph_proxy.attach_run_stream` / stream mirror / task routers / `run_status_reconcile`；迁移后保留业务事件语义，不绑定 LangGraph frame。

### 5.2 为什么不是先做普通聊天

普通聊天主要验证单一 session 的输入输出；无人值守链能一次验证：

- AgentScope planning 和执行 run 的边界；
- Run、主任务、子任务和审批状态同步；
- 后台 scheduler / task queue 与多实例幂等；
- 重试、超时、取消和重启恢复；
- SSE 事件、断线续接、stream mirror 和 terminal 收敛；
- 结果/资产元数据的正式落库。

### 5.3 建议的切片验收条件

- 一个任务只能被一个 worker claim；重复 tick 不得创建重复 planning run 或重复子任务。
- planning 完成后，计划和依赖 DAG 可从 PostgreSQL 重建；AgentScope 状态丢失不影响业务记录。
- 未授权子任务不得执行；审批回调重复投递不会重复放行。
- 每个 Run 只有一个终态；terminal 事件、任务 finalize、session idle 更新幂等。
- SSE 断线后按 `(run_id, seq/cursor)` 可重放，重复 frame 不导致重复消息或重复资产。
- 进程重启后可发现 queued/running/waiting_approval 的任务并恢复或明确标记失败。
- 成功、失败、取消、超时都能在 Task Center/API 查询到同一结果。
- 如包含本地动作，必须额外验证设备命令唯一 ID、回执、断线和重复执行保护；当前代码尚无该协议。

---

## 6. 风险与待确认项

### 6.1 代码已证实

1. LangGraph graph 配置、SDK、进程内/外部 proxy、SSE attach、heartbeat、active stream registry 和 startup reconcile 均存在；定位见第 1 节。
2. 业务状态分散在 chat、app run、automation run、collab task/subtask、goal/mission、approval、artifact/media 表中；没有单一 Run 状态来源。
3. `/Users/wangjiaquan/project/QAgent/backend/app/gateway/run_status_reconcile.py` 依赖 LangGraph 探测来修复 SQLite session 状态，说明当前业务状态与框架状态存在反向推断。
4. 自动化存在 bound app、direct LangGraph、unattended/task-center 三条路径；scheduler 默认启动且有进程内 semaphore。
5. SQLite 只支持 `StorageConfig.backend="sqlite"`；应用库与 LangGraph checkpoint 通常分库；schema public epoch 已 squash 到 1。
6. SSE 已实现 heartbeat、slim/dedupe、镜像和 refresh attach；raw frame 直接进入 SQLite，事件格式迁移会触发数据/协议风险。
7. 本地执行有 desktop stdio 和 built-in command/process tools，但没有被当前源码证实的统一设备命令生命周期。

### 6.2 需要业务确认

1. `/api/langgraph` 是否必须对现有前端/第三方客户端长期兼容；若兼容，兼容的是 URL、JSON、SSE frame 还是仅业务语义？
2. `lead_agent`、`goal_agent`、`claude_code_chat` 的产品职责和 AgentScope 2.0 中的 agent/team 拆分是否一一对应？
3. Goal/Mission 表中的哪些字段是企业正式记录，哪些是可重建的推理缓存？是否需要保留完整 turn/plan/feedback 历史？
4. 一个 session 是否允许并发多个 Run？普通聊天、Goal、协同任务、自动化是否共用同一个 session/thread？
5. 自动化的正式来源是 SQLite 表、TOML 文件还是两者兼容期并存；`once_fired`、cron/rrule、时区和重复触发语义由谁负责？
6. 无人值守任务是否默认自动授权（当前代码对 automation 有 `execution_authorized=True` 语义），哪些风险动作必须二次审批？
7. 设备命令是否属于阶段 1.5/P0；哪些工具必须在员工设备执行，哪些可以云端执行？
8. artifact 原件是否允许进入 PostgreSQL，还是只保存企业文件存储引用；媒体的远端 URL 是否需要下载/代理/权限包装？
9. 任务/Run/审批/资产的状态展示是否允许统一命名，还是必须兼容当前 UI 的多种值？
10. 多员工切换设备时，会话、任务、审批和本地工作区的可见性及 owner/scope 规则是什么？

### 6.3 需要运维确认

1. 客户私有化部署的 PostgreSQL 版本、HA/备份/RPO/RTO、连接池和迁移窗口；是否允许在线双写或停写迁移？
2. QAgent 是单实例还是多实例 Gateway/worker；自动化 scheduler 是否保证单 leader，是否已有分布式锁、lease、fencing 或消息队列？
3. AgentScope worker 的部署拓扑、进程模型、水平扩展、任务可见性、心跳和优雅停机策略。
4. LangGraph checkpoint 数据的保留期、迁移/丢弃策略，以及旧库与新 PostgreSQL 的并行运行时间。
5. SSE 经历的 Nginx/Ingress/LB idle timeout、最大连接时长、缓冲策略和重连代理；heartbeat 的实际运维下限。
6. 本地设备在线发现、出站/入站网络、证书/凭据/KMS、设备身份、命令回执和断线重试策略。
7. 文件原件、NAS、对象存储的备份、权限、生命周期和跨设备可访问性；`local_path` 在云端是否可达。
8. SQLite 现有文件的位置、实例数量、WAL/SHM 大小、可用磁盘和历史数据保留期；当前代码扫描尚未取得真实样本容量。
9. 生产日志/监控是否采集 run/task/worker/terminal/cursor 指标；迁移期间如何对比新旧 Runtime 的成功率、延迟和丢事件。

### 6.4 当前阻塞项

- **没有真实生产/客户样本数据库的行数、容量和时间范围**：因此无法给出迁移规模、停写时长或 PostgreSQL 容量结论；需要运维提供一致性快照后按第 2.3 节 SQL 盘点。
- **没有设备命令正式协议的源码证据**：如果阶段 1.5 必须包含设备动作，需要业务和运维先确认命令 envelope、设备身份、回执和幂等策略。
- **没有确定 `/api/langgraph` 兼容承诺和多实例调度模型**：这两项直接影响是做兼容适配层还是立即改 URL，以及是否需要 queue/lease/fencing 作为 P0 基础设施。

---

## 7. 阶段 1 开工清单

1. 固化 Runtime facade 的接口和事件 envelope，先实现 LangGraph adapter，再实现 AgentScope adapter。
2. 固化 canonical Run/Task/Approval/Asset 状态机、状态转换表、幂等键和 terminal 规则。
3. 为 PostgreSQL 建立与现有表的映射矩阵：主键、外键、nullable、时间精度、JSONB、大字段/原件策略、索引和迁移回滚。
4. 先为无人值守主链增加 claim/lease/version 条件更新和可恢复事件记录，再接 AgentScope。
5. 把 SSE stream mirror 从“LangGraph raw frame”改成版本化 QAgent event；保留旧 frame 只作为过渡兼容数据。
6. 将 `/Users/wangjiaquan/project/QAgent/backend/app/gateway/run_status_reconcile.py` 的“探测框架反推业务状态”改为“读取正式 Run 状态 + 观测 AgentScope worker 心跳”。
7. 盘点并标记所有 `langgraph_*` 字段和 TOML 配置的保留、兼容、迁移、删除计划；在未完成验收前不要删依赖。
