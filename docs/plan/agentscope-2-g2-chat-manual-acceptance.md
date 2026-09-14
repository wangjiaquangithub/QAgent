# G2 Chat / Live Run 接入人工验收步骤

> 卡片：AG-G2-CHAT-005-A01
> 分支：`codex/g2-chat-live-run`　基线：`origin/codex/agentscope-runtime-migration`（`b09adf4`）
> 前置阅读：`agentscope-2-g2-chat-entry-map.md`（真实链路盘点）
> 性质：**验收步骤说明**。本文档只描述如何人工确认 G2 Chat 适配层的行为，不含业务改动。

本文档面向「明天统一推送后」的执行者，回答一个问题：**怎么在不破坏现有聊天链路的前提下，逐步确认 Chat 域已经可以走 QAgent Runtime。**

---

## 1. 验收对象

本阶段不改造聊天主链路，只新增了四个 **Chat 域本地适配模块**（全部位于 `backend/app/gateway/`，均未触碰 `qagent_runtime`、migration、Gateway 主干与前端）：

| 模块 | 职责 | 卡片 |
|---|---|---|
| `chat_runtime_identity.py` | Chat 会话/消息标识 → Runtime 契约标识（`org_id` 由服务端身份导出） | 002-A01 |
| `chat_runtime_dispatch.py` | Chat → Runtime 的最小派发缝，默认关闭，可一键回退 | 003-A01 |
| `chat_runtime_result.py` | Runtime 结果 → Chat 兼容投影（不复制第二份执行真相） | 003-A02 |
| `chat_runtime_stream_bridge.py` | Runtime Event v1 → 现有 Chat SSE 帧（有序 / 终态一次 / cursor 续读） | 004-A01、004-A02 |

对应测试：`backend/app/tests/test_g2_chat_runtime_{identity,dispatch,result,stream_bridge}.py`。

> **核心设计约束**：`org_id` **只能**由服务端已验身份导出。`chat_runtime_identity` 刻意不提供任何让调用方传入组织的参数，因此客户端无法伪造组织范围。

---

## 2. 验收前提

1. 分支已就位并推送：

   ```bash
   cd /Users/wangjiaquan/project/QAgent
   git switch codex/g2-chat-live-run      # 或 git switch -c 追踪远端
   git log --oneline -6
   ```

   期望看到（从新到旧）：

   ```
   test(chat): cover cancel and reconnect convergence
   feat(chat): bridge runtime events to live stream
   feat(chat): project runtime results into live messages
   feat(chat): route live runs through runtime adapter
   feat(chat): map live sessions to runtime identities
   test(chat): protect existing live run entry contract
   docs(chat): map live run integration entrypoints
   ```

2. 依赖已安装（`uv sync --group dev`），且 **PostgreSQL 已按 `agentscope-2-postgres-foundation-runbook.md` 启动并完成 migration**——Runtime 的 `create_run` 需要真实数据库。

3. **验收期间的开关默认关闭**，聊天仍走旧 LangGraph 链路。只有第 5 节才显式打开。

---

## 3. 第一层：离线测试（无数据库、无网络）

这是最快的门禁，任何一步失败都应停止，不要继续。

```bash
cd /Users/wangjiaquan/project/QAgent/backend

# 3.1 四个 G2 适配层测试
uv run pytest \
  app/tests/test_g2_chat_runtime_identity.py \
  app/tests/test_g2_chat_runtime_dispatch.py \
  app/tests/test_g2_chat_runtime_result.py \
  app/tests/test_g2_chat_runtime_stream_bridge.py -q
```

**期望**：`81 passed`。

```bash
# 3.2 聊天入口保护性契约测试（需要完整 agent runtime 依赖）
uv run pytest packages/harness/tests/test_g2_chat_append_contract.py -q
```

**期望**：7 passed。若本机缺少 `langgraph`/`langchain`，其中 3 个写路径测试会 **skip**（带 `requires_agent_runtime` 原因），这是**有意为之**的诚实门禁，**不是通过**。请确认 skip 原因字符串包含 `agent runtime deps`，不要把它当成绿灯。

**验收判据（第一层）**

- [ ] 81 passed，0 failed
- [ ] 入口契约测试无 failed；若有 skip，原因是依赖缺失而非断言跳过

> ⚠️ **禁止**为了让测试变绿而安装/伪造 `langgraph` 之外的东西，或删除 skip 门禁。skip 是有意标注的环境事实。

---

## 4. 第二层：开关关闭时的零影响验证（最重要的一步）

**目的**：证明「没开开关 = 现有聊天链路完全不变」。

1. 确认环境变量未设置（或为空）：

   ```bash
   echo "QAGENT_CHAT_RUNTIME_OPT_IN='${QAGENT_CHAT_RUNTIME_OPT_IN:-<unset>}'"
   ```

   期望 `<unset>`，或任意**非真值**（`0`、`false`、`off`、空串）。真值仅 `1` / `true` / `yes` / `on`（大小写不敏感）。

2. 启动 Gateway，走一次**真实聊天**：打开现有聊天界面，发送一条普通消息，确认：

   - [ ] 请求仍打到 `/api/langgraph/threads/{threadId}/runs/stream`（旧链路）
   - [ ] 流式输出、最终结果、断线重连行为**与改动前完全一致**
   - [ ] 没有出现任何 Runtime 相关的新请求（`/api/qagent/runtime/*`）

**判据**：开关关闭时，`dispatch_chat_run()` 返回 `enabled=False, dispatched=False, reason="opt_in_disabled"`，且**不触碰 Runtime 的任何状态**。旧链路零影响是本卡片的硬要求。

---

## 5. 第三层：打开开关后的顺序推进

> **只在一个受控的本地/开发环境执行。** 生产库、共享库一律不在此列。

### 5.1 打开开关

```bash
export QAGENT_CHAT_RUNTIME_OPT_IN=1
```

重启 Gateway。此时发送一条聊天消息应观察到：

- [ ] Runtime 侧产生一条 run（`run.created` → …），`org_id` 形如 `identity:<type>:<id>`，**不是**客户端传来的任何值
- [ ] 同一个会话的同一条消息重复发送时，**不会**产生第二条 run（幂等键 `(org_id, idempotency_key)` 唯一约束生效）
- [ ] Runtime 不可用时（例如故意停掉 PostgreSQL），聊天**不报 500**、**不崩溃**，而是回落旧链路（`reason="runtime_unavailable"`）

### 5.2 事件 → SSE 帧映射确认

对一次真实 run，确认前端收到的 SSE 帧序列满足：

| Runtime 事件 | 前端 SSE 事件名 | 说明 |
|---|---|---|
| `run.created` / `run.queued` / `run.planning` / `run.waiting_approval` / `approval.*` / `run.running` / `run.executing` / `asset.available` | `runStatus` | 过程态 |
| `run.completed` | `runCompleted` | 成功终态 |
| `run.cancelled` | `runCompleted` | 取消也收敛到同一个终态帧 |
| `run.failed` | `error` | 失败走既有错误通道 |
| （流结束） | `done` | `close()` 幂等补发，**只有一次** |

**判据**：

- [ ] 每个帧形如 `event: <name>\ndata: <json>\n\n`，与现有前端解析器一致
- [ ] `id:` 行携带 Runtime `sequence`，且**严格递增**
- [ ] **一次流内终态帧恰好出现一次**（`runCompleted` 或 `error`，不会两者都来，也不会重复）
- [ ] `done` 帧**恰好一次**，且是最后一个

### 5.3 取消语义确认

1. 发起一次长 run，然后在执行中触发取消。
2. 期望：收到 `run.cancelled` → 前端 `runCompleted`（`status: "cancelled"`）→ `done`，流关闭。
3. 若取消**晚于**完成到达：**不得**再发第二个终态帧（终态不倒退）。

**判据**

- [ ] 取消先到：收敛为一次 `runCompleted`，`status="cancelled"`
- [ ] 取消后到：被丢弃，无第二个终态帧

### 5.4 断线续读确认（cursor / attach）

1. 发起一次 run，在收到约 2 个帧后**强制断开**连接（关闭页面 / 断网 / 杀进程）。
2. 用客户端已有的续读入口重连：`/api/chat/sessions/{sessionKey}/stream-resume`，携带 `afterSeq`（= 断线前最后收到的 `sequence`）。
3. 期望：

   - [ ] 重连**不回放**已收过的帧（`sequence <= afterSeq` 一律丢弃）
   - [ ] 重连**不丢帧**（从 `afterSeq + 1` 继续）
   - [ ] 重连后**整条会话累计**只有一次终态帧、一次 `done`
   - [ ] 若重连时 run 已终态，则只收到 `done`（无重复终态）

**判据**

- [ ] 分段续读拼起来 = 一次完整 run，无重复 sequence、无空洞
- [ ] 终态帧在整个（可能多次重连的）会话中只被用户看到一次

---

## 6. 回退

任一步不符合预期，**立即回退，不要在生产语义下继续调**：

```bash
unset QAGENT_CHAT_RUNTIME_OPT_IN
# 重启 Gateway
```

回退后聊天链路应完全恢复到第 4 节的状态（旧 LangGraph 路径，零 Runtime 请求）。

**注意**：关闭开关**不会**删除已经产生的 Runtime run。这些 run 是 Runtime 的正常执行事实，与聊天链路无关；如需清理，走 Runtime 自身的运维路径，不要在 Chat 域做隐蔽删除。

---

## 7. 明确不在本次验收范围

以下均**不在**本阶段验收，看到它们不等于本次失败：

- LangGraph 主链路的删除/改写（`/api/langgraph` 保持原样）
- `qagent_runtime` 内部实现、event 模型、migration 的任何改动
- 全局 SSE registry、Gateway 共享基础设施的改动
- 前端 `evopanel` 的改动（本次**零前端改动**，复用的是已有帧格式与已有续读机制）
- Automation / Scheduler / 任务中心 / 无人值守任务域
- 设备协议 / lease / fencing

---

## 8. 红线条目

- **禁止**在 `qagent_dev`、`qagent_prod`、`qagent_production`、`qagent_test` 或任何共享库上做破坏性验证。破坏性验证只允许 `qagent_foundation_test`（见 PostgreSQL Runbook）。
- **禁止**绕过 `org_id` 的服务端导出逻辑，或新增任何让客户端传入组织的参数。
- **禁止**为了让测试通过而删除 `requires_agent_runtime` skip 门禁。
- **禁止**在 Chat 域复制第二份可变执行真相（Runtime 是唯一执行事实源，`chat_*` 字段只是兼容投影）。
- **禁止**在没有 `QAGENT_CHAT_RUNTIME_OPT_IN` 显式打开的情况下把聊天流量引到 Runtime。

---

## 9. 验收记录表

| 层 | 检查项 | 结果 | 备注 |
|---|---|---|---|
| 1 | 四个 G2 测试 `81 passed` | ☐ | |
| 1 | 入口契约测试无 failed（skip 需说明原因） | ☐ | |
| 2 | 开关关闭 = 零 Runtime 请求、旧链路不变 | ☐ | |
| 3.1 | run 产生，`org_id` 来自服务端身份 | ☐ | |
| 3.1 | 同消息不产生第二条 run（幂等） | ☐ | |
| 3.1 | Runtime 不可用时回落而不报错 | ☐ | |
| 3.2 | 帧形状 / `id:` 递增 / 终态一次 / `done` 一次 | ☐ | |
| 3.3 | 取消先到收敛一次；取消后到被丢弃 | ☐ | |
| 3.4 | 重连无回放、无丢帧、累计终态一次 | ☐ | |
| 6 | 回退后恢复旧链路 | ☐ | |
