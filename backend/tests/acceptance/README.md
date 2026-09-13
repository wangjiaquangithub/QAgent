# AgentScope 2 验收测试脚手架

本目录只承载 AgentScope 2 替换、PostgreSQL 正式状态和本地设备协议的**验收测试准备工作**。
当前阶段不调用尚未确定的 Runtime、数据库或设备接口，也不模拟一套会掩盖真实契约的假实现。

## 约定

- 测试以 `agentscope_acceptance` marker 标记，完成阶段 0 和阶段 1.5 的接口冻结后再接入真实集成 fixture。
- `test_data.py` 只生成可复现的测试 ID 和证据目录，不创建生产数据。
- 每个验收用例必须关联 `org_id`、`run_id`、`command_id` 或 `migration_batch_id`（按场景适用），并把日志、事件序列、数据库快照 / SQL、故障注入时间线和最终结论写入证据目录。
- 集成测试默认使用隔离 PostgreSQL、可控的 fake device transport 和可重置的资产目录；不得复用开发机正式数据库或真实企业资产。
- 需要真实进程重启、备份恢复或部署扫描的用例，归入人工演练或 CI / 发布流水线，而不是在单元测试中伪造。

## 运行方式（接口落地后）

```bash
cd backend
uv run pytest tests/acceptance -m agentscope_acceptance -v
```

当前没有验收实现测试；目录和 fixture 的存在不代表任何迁移或 Runtime 能力已经完成。
