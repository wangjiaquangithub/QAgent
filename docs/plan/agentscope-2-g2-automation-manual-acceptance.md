# G2 Automation / Task Center → Runtime 人工验收说明

- 卡片：`AG-G2-AUTO-006-B02`（对应母任务 `G2-AUTO-005/006` 的人工验收部分）
- 分支：`codex/g2-automation-task-center`
- 覆盖范围：**仅**已实现并已有测试的「unattended task opt-in → QAgent Runtime Run → Runtime 事件回写 Task Center 状态/历史 → 取消」这一条切口。
- 明确**不含**（未迁移，勿在验收中当作已实现）：App Runner 绑定工作流路径、prompt-only 规则默认直连 LangGraph 路径、scheduler 定时触发路径、多实例 HA / leader / lease / fencing、设备协议能力、任何前端页面改动。

---

## 1. 环境开关（唯一开关，服务端读取）

| 项 | 值 |
|---|---|
| 环境变量 | `EVOFLOW_AUTOMATION_UNATTENDED_RUNTIME` |
| 视为开启 | `1` / `true` / `yes` / `on`（大小写不敏感） |
| 默认 | **关闭**（未设置、空串、`0`、`false`、`off` 均为关闭） |
| 读取位置 | `backend/app/gateway/task_runtime_optin.py`，仅服务端读取，客户端无法影响 |

开关只影响 **Runtime opt-in 分支**；取消路径不受它控制（见 §6）。

---

## 2. 验收前准备

1. 进入工作树与分支：

   ```
   cd <你的路径>/QAgent-g2-automation
   git log -1 --oneline    # 应为 codex/g2-automation-task-center 的最新提交
   ```

2. 跑本切口的全部直接测试（离线，无需网络、无需 API key、不产生模型费用）：

   ```
   cd backend
   PYTHONPATH=".:packages/harness" python -m pytest \
     app/tests/test_task_runtime_context.py \
     app/tests/test_task_runtime_linkage.py \
     app/tests/test_task_runtime_optin.py \
     app/tests/test_task_runtime_projection.py \
     app/tests/test_task_runtime_event_bridge.py \
     app/tests/test_task_runtime_cancel.py \
     app/tests/test_task_runtime_e2e_boundary.py -q
   ```

   全部测试使用 in-process 的 fake Runtime contract，**不访问网络、不读 API key、不调用付费模型**。

---

## 3. 用例 A：开关关闭时旧行为不变（最重要）

1. 确保**不**设置 `EVOFLOW_AUTOMATION_UNATTENDED_RUNTIME`。
2. 从既有 Task Center 入口创建并触发一条无人值守任务（`run_mode=unattended`），例如经既有 `POST /api/tasks` + 既有队列推进（`POST /api/tasks/queue/tick`，或等待既有队列循环）。
3. 预期：
   - 任务状态流转与开关引入前**完全一致**（仍走原有 LangGraph / 旧执行链）；
   - 任务的 `execution_history` 中**不出现**任何 `source = qagent_runtime` 的记录；
   - 任务行的兼容关联字段中**不出现** `runtime_run_linkage`；
   - PostgreSQL 的 `qagent_runs` **不新增**该任务对应的 Run。

---

## 4. 用例 B：开关开启且任务已授权时创建/复用 Runtime Run

1. 设置开关：

   ```
   export EVOFLOW_AUTOMATION_UNATTENDED_RUNTIME=1
   ```

2. 准备一条**已授权**（`execution_authorized = true`）且 `run_mode=unattended` 的任务。
3. 触发一次队列推进。
4. 预期：
   - PostgreSQL `qagent_runs` 新增 **1** 条该任务的 Run；
   - 任务行出现兼容关联 `runtime_run_linkage`，其中 `runtime_run_id` 与上面的 Run 一致；
   - 任务状态按 Runtime 状态收敛（见 §5）。
5. **重复触发**同一条任务（同一次 attempt）：预期**不新增第二条 Run**，`runtime_run_id` 保持不变——即重复触发只复用同一个关联。
6. **前置不足时保持原路径**：若任务未授权、或服务端无法从任务 ACL 得到可信身份、或 Runtime 服务不可用，则该次推进仍走原有旧路径，且不产生任何 Runtime 副作用。

---

## 5. Runtime 状态 → Task Center 预期

| Runtime 状态 | Task 状态 | execution_history |
|---|---|---|
| `waiting_approval` | **不变**（既不是 `paused` 也不是 `planned`） | 追加一条，含 `runtime_status=waiting_approval`、`approval_required=true`、`source=qagent_runtime` |
| `created` / `queued` | `pending` | 追加对应记录 |
| `planning` | `planning` | 追加对应记录 |
| `running` / `executing` | `executing` | 追加对应记录 |
| `completed` | `completed` | 追加对应记录 |
| `failed` | `failed` | 追加对应记录，含脱敏 `error_code` / `reason` |
| `cancelled` | `cancelled` | 追加对应记录 |
| `timed_out` | `failed` | 追加记录，**保留** `runtime_status=timed_out` + 脱敏 `error_code` / `reason` |

收敛与不倒退规则：

- 同一事件重复投递（相同 `event_id` / 相同 sequence）：**不重复追加**，状态不变。
- 较旧 sequence 的事件：**不改变**任务状态。
- 任务已处于终态（`completed`/`failed`/`cancelled`）：**终态不被覆盖**，旧执行器无法把它改回 `completed`。
- 未终止的事件**不能**把已终止的任务重新激活。

### 观察「waiting_approval 状态不变但历史可见」

- 状态：读任务详情（既有 `GET /api/tasks/{task_id}`）→ `status` 与进入审批前一致；
- 历史：读 **既有** `GET /api/tasks/{task_id}/execution-history` → `execution_history` 末条含 `runtime_status = "waiting_approval"` 与 `approval_required = true`。

该历史列表由既有接口**原样返回**，未改动全局 SSE、未改动事件 schema、未改前端。

---

## 6. 用例 C：取消

1. 对**已关联 Runtime Run** 的任务调用既有取消入口：

   ```
   POST /api/tasks/{task_id}/cancel
   ```

2. 预期：
   - 该任务关联的 Runtime Run 收到一次**公开取消调用**（`request_cancel(run_id)`）；本实现不写 Runtime 任何表；
   - 任务状态为 `cancelled`，`execution_history` 追加一条 `runtime_status=cancelled`；
   - **重复取消**：第二次不再向 Runtime 发起调用（幂等）；
   - 取消后，即使旧事件/旧执行器再次投递 `completed`，任务仍是 `cancelled`（终态不被覆盖）。
3. 对**未关联 Runtime** 的任务取消：行为与改动前完全一致（旧取消路径），且不产生任何 Runtime 调用。

> 取消**不受** `EVOFLOW_AUTOMATION_UNATTENDED_RUNTIME` 开关控制，也不受 `execution_authorized` 门控——取消正是撤销该授权的手段。

---

## 7. 如何核对两边的真实数据

### 7.1 PostgreSQL：Runtime 的唯一事实源

任务关联的 Run（把所有 `<task_id>`、`<org>` 换成实际值）：

```sql
-- Run 本体
SELECT run_id, org_id, task_id, status, idempotency_key, created_at, updated_at
FROM qagent_runs
WHERE task_id = '<runtime_task_id>'
ORDER BY created_at DESC;

-- 该 Run 的事件序列（用于核对状态收敛）
SELECT sequence, type, occurred_at
FROM qagent_run_events
WHERE run_id = '<run_id>'
ORDER BY sequence;
```

> `runtime_task_id` 与 Task Center 的任务 id **不同**：它是按组织分区的确定性映射值，与任务行兼容关联中的 `task_id` 一起出现，可直接从 §7.2 查到。

### 7.2 SQLite：仅保存兼容关联

Task Center 的任务仍在既有 SQLite 存储中。关联保存在任务行**既有**的扩展字段槽位（`evoflow_collab_tasks.extra_json` 中的 `runtime_run_linkage`）：

```sql
-- 找到关联（extra_json 为 JSON 文本）
SELECT main_task_id, task_id, status,
       json_extract(extra_json, '$.runtime_run_linkage.runtime_run_id')  AS runtime_run_id,
       json_extract(extra_json, '$.runtime_run_linkage.org_scope_key')   AS org_scope_key,
       json_extract(extra_json, '$.runtime_run_linkage.idempotency_key') AS idempotency_key
FROM evoflow_collab_tasks
WHERE task_id = '<task_id>';
```

执行历史（每条记录即一个 JSON）：

```sql
SELECT parent_task_id, sort_order, event_json
FROM evoflow_task_exec_history
WHERE parent_task_id = '<task_id>'
ORDER BY sort_order;
```

**边界（必须遵守）**：SQLite 里的关联只是**兼容指针**。Runtime 的 Run、Event、Result、Asset、Recovery Point 一律以 PostgreSQL 为唯一事实源；不得把 Runtime 状态、事件或结果迁回 SQLite 作为第二事实源，也不存在为迁移而新建的表或 migration。

---

## 8. 回退方式

1. 关闭开关即可回到旧路径：

   ```
   unset EVOFLOW_AUTOMATION_UNATTENDED_RUNTIME
   ```

   （或显式设为 `0` / `false` / `off`。）

2. **不删除任何历史数据**：已产生的 `qagent_runs` 事件、任务行上的 `runtime_run_linkage`、以及 `execution_history` 中的历史记录都保留原样。关闭开关后新触发的任务不再创建 Run，既有记录仅为审计线索。

3. 取消路径不走开关；如需让某条已关联任务彻底停止，仍使用既有取消入口（§6）。

---

## 9. 已知风险与缺口（请人工验收时重点确认）

| 级别 | 内容 | 精确位置 | 最小修复 |
|---|---|---|---|
| **P1** | 无人值守路径调用 Runtime 时**未传组织**：`RuntimeService.create_run` 的公开签名是 `create_run(*, org_id="local", task_id, input_payload, idempotency_key)`，而本域使用的 `RuntimeRunContract` 协议只声明了后三个参数。结果是经此路径创建的 Run，其 `qagent_runs.org_id` 会落到默认值 `"local"`，而不是任务所属组织。跨组织的幂等空间**不会**因此串号（本域的 `idempotency_key` 已按组织分区，且关联读取会拒绝跨组织），但 Run 行的组织归属不正确，任何按 `org_id` 过滤的 Runtime 侧查询都会不一致。 | `backend/app/gateway/task_runtime_optin.py`（`establish_runtime_run` 的 `create_run` 调用）与 `backend/app/qagent_runtime/contract.py`（协议未声明 `org_id`） | 在 `RuntimeRunContract` 协议中声明 `org_id`，并在调用处传入由可信上下文派生的组织值（含在 `TaskRuntimeContext` 中）。**本卡为「测试 + 文档」卡，未改代码，待批准后单独开卡修复。** |
| P2 | 应用重启后，已创建但未跑完的 Run 需要靠 Runtime 侧既有恢复能力继续；本域目前**未**实现「重启后把 Runtime 状态重新投影回 Task Center」的定时对账。 | 本域未实现；Runtime 侧已有 `recover_incomplete_runs` | 后续卡补一条对账/回投影，禁止在 SQLite 侧新建状态源。 |
| P3 | `execution_history` 是自由结构列表，追加记录不校验既有消费者的字段假设（当前读取方均以 `.get` 容错）。 | `backend/app/gateway/task_runtime_projection.py` | 无阻塞；若后续前端要展示审批态，建议按既有历史字段渲染，不改状态枚举。 |

---

## 10. 本说明未覆盖的能力（勿误判为已迁移）

- App Runner / 绑定已发布工作流的自动化路径；
- prompt-only 规则的默认直连 LangGraph 路径；
- scheduler 定时触发路径（`automation_tick` / `run_automation_scheduler`）；
- 多实例一致性（leader / lease / fencing）、HA、压测、Chaos；
- 设备协议与离线回执；
- 任何前端页面或全局 SSE / 流协议改动。
