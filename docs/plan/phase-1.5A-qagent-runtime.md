# 阶段 1.5A：服务端最小纵向闭环

## 目标与范围

阶段 1.5A 新增一条独立的 QAgent Runtime 主链，完成无人值守任务的服务端闭环：

```text
创建 Run → AgentScope 规划 → 等待审批 → 审批通过 → 服务端子任务 → 结果/资产元数据 → QAgent SSE
```

本阶段不接入本地设备命令，不设计桌面 Agent 协议，也不删除或替换现有 LangGraph 链路。

## 新旧链路边界

- 新链路入口为 `/api/qagent/runtime`，正式 ID 使用 `run_id`、`task_id`、`event_id/sequence` 和 `approval_id`。
- PostgreSQL 是新链路的唯一正式状态来源；服务重启恢复通过 `qagent_runs` 查询未完成 Run，不依赖内存 stream registry、LangGraph thread/run 或 SQLite 历史数据。
- 现有聊天、`/api/langgraph`、Automation 和相关 SQLite 状态继续保留，作为迁移期旧链路和回退路径。
- 新 Runtime 业务代码不调用 LangGraph `runs.wait`/`runs.stream`，也不把 LangGraph 事件帧直接输出到 SSE。

## API

前缀：`/api/qagent/runtime`

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| POST | `/runs` | 创建 Run；可传 `task_id`、`input_payload`、`idempotency_key` |
| POST | `/runs/{run_id}/start` | 启动规划 |
| GET | `/runs/{run_id}` | 查询 Run 状态、计划和审批 |
| GET | `/runs/{run_id}/events?after_sequence=0` | 读取 QAgent 事件 SSE |
| POST | `/runs/{run_id}/cancel` | 请求取消 |
| POST | `/runs/{run_id}/resume` | 从 PostgreSQL 恢复未完成 Run |
| GET | `/runs/{run_id}/result` | 查询结果、资产和错误 |
| POST | `/runs/{run_id}/approvals/{approval_id}/grant` | 通过审批 |
| POST | `/runs/{run_id}/approvals/{approval_id}/reject` | 拒绝审批 |

示例：

```bash
# 创建
curl -X POST http://localhost:8000/api/qagent/runtime/runs \
  -H 'content-type: application/json' \
  -d '{"task_id":"task-001","input_payload":{"prompt":"生成服务端报告"}}'

# 启动规划
curl -X POST http://localhost:8000/api/qagent/runtime/runs/<run_id>/start

# 通过审批（从状态响应中取得 approval_id）
curl -X POST http://localhost:8000/api/qagent/runtime/runs/<run_id>/approvals/<approval_id>/grant \
  -H 'content-type: application/json' \
  -d '{"decided_by":"operator","reason":"approved"}'

# 订阅事件
curl -N 'http://localhost:8000/api/qagent/runtime/runs/<run_id>/events?after_sequence=0'
```

SSE 是 QAgent Event Protocol v1 格式，不透传 AgentScope 或 LangGraph 私有帧：

```text
event: run.created
id: evt_...
data: {"event_id":"evt_...","run_id":"run_...","sequence":1,"occurred_at":"2026-09-13T...Z","type":"run.created","payload":{"task_id":"task-001"}}
```

## QAgent Event Protocol v1

每个事件都包含 `event_id`、`run_id`、`sequence`、`occurred_at`、`type` 和 `payload`。同一 Run 的 `sequence` 从 1 开始严格递增，并持久化在 PostgreSQL `qagent_run_events` 中。

事件类型：

- `run.created`
- `run.queued`
- `run.planning`
- `run.waiting_approval`
- `approval.granted`
- `approval.rejected`
- `run.running`
- `run.completed`
- `run.failed`
- `run.cancelled`
- `asset.available`

状态机支持：`created`、`queued`、`planning`、`waiting_approval`、`running`、`completed`、`failed`、`cancelled`、`timed_out`。

## PostgreSQL 表职责

独立迁移 `backend/migrations/versions/0002_qagent_runtime.py` 位于 `0001_postgres_foundation` 之后：

- `qagent_runs`：Run 主记录、Task 关联、状态/版本、输入、计划、结果、错误、恢复上下文、时间戳和恢复次数。
- `qagent_run_events`：不可变 QAgent 事件流；按 `(run_id, sequence)` 唯一约束保证顺序。
- `qagent_approvals`：审批请求、状态、审批人、原因及决定时间。
- `qagent_recovery_points`：计划完成等可恢复位置和上下文；同一 Run/checkpoint key 幂等。
- `qagent_run_assets`：新链路产生的资产元数据引用，不保存设备执行协议。

## AgentScope Adapter 替换边界

`backend/app/qagent_runtime/agentscope_adapter.py` 是 AgentScope 唯一规划适配边界。Adapter 只返回 QAgent 自己的 `PlanResult`，不让 AgentScope 私有对象、checkpoint ID 或事件格式越过边界。当前 AgentScope 未安装时使用 deterministic fallback planner，保证本地测试和服务端最小闭环可运行。

`backend/app/qagent_runtime/executor.py` 是服务端执行器边界。当前 `ServerSubtaskExecutor` 只生成结构化服务端结果和资产引用；下一阶段接入设备能力时，应替换/扩展该执行器，而不修改 Runtime Contract、数据库事件协议或 SSE 格式。

## 本地启动、迁移、测试与验证

设置 PostgreSQL 连接配置后执行：

```bash
cd backend
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --reload
```

运行阶段 1.5A 单元/路由测试（测试使用注入的 SQLite engine，仅用于隔离测试，不是生产状态源）：

```bash
cd backend
.venv/bin/pytest -q app/tests/test_qagent_runtime.py app/tests/test_qagent_runtime_router.py
```

运行格式、静态检查和编译检查：

```bash
cd backend
.venv/bin/ruff format app/qagent_runtime migrations/versions/0002_qagent_runtime.py app/tests/test_qagent_runtime.py app/tests/test_qagent_runtime_router.py
.venv/bin/ruff check app/qagent_runtime migrations/versions/0002_qagent_runtime.py app/tests/test_qagent_runtime.py app/tests/test_qagent_runtime_router.py
.venv/bin/python -m compileall app/qagent_runtime migrations/versions/0002_qagent_runtime.py app/tests/test_qagent_runtime.py app/tests/test_qagent_runtime_router.py
```

验证旧 LangGraph 路径没有被破坏：

```bash
.venv/bin/pytest -q app/tests/test_lazy_langgraph_proxy.py
```

启动时 `background_startup` 会从 PostgreSQL 扫描 `created/queued/planning/waiting_approval/running` Run 并调用 Runtime 的恢复入口；恢复操作对已写入计划、审批、结果和资产具有幂等保护。

## 明确未做的能力

- 本阶段不发送本地设备 Command。
- 本阶段不实现设备 ACK、超时重试、离线重连或桌面 Agent 协议。
- 本阶段不迁移全量 SQLite 历史数据。
- 本阶段不替换现有聊天、`/api/langgraph` 或 Automation 旧路径。

下一阶段接入设备协议的位置是 `backend/app/qagent_runtime/executor.py` 的 `ServerSubtaskExecutor`；设备 Command/ACK/离线重连的接口应在该执行器之后增加，继续由 Runtime Contract 只接收稳定的执行结果与资产元数据。
