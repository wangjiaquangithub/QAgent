# AgentScope 2.x SDK 兼容性验证

- 验证日期：2026-09-13
- 选定版本：`agentscope==2.0.8`
- 验证范围：依赖可安装性、真实 SDK API、离线 fake provider 单 Agent 单 turn、结构化输出和错误边界
- 明确不在本轮范围：QAgent Runtime 主链、Approved Run 回写 Result / Asset / Event / Recovery Point，以及真实生产模型调用

## 版本与官方资料

本项目在 `backend/pyproject.toml` 中精确声明 `agentscope==2.0.8`，并在 `backend/uv.lock` 中锁定同一版本。选择理由：

1. `2.0.8` 是明确的 AgentScope 2.x 版本，且当前项目 Python 要求 `>=3.12`，已在本项目 `uv` 环境中成功安装。
2. 本版本的真实 API 已通过本页配套的离线 probe 执行验证，而不是只验证 import。
3. 它提供统一的 `agentscope.agent.Agent`、`agentscope.message.Msg`、`ChatModelBase` 和 `structured_schema` API，足以支撑后续最小 server-only 单 Agent 链路。

官方资料：

- AgentScope 官方仓库：[agentscope-ai/agentscope](https://github.com/agentscope-ai/agentscope)
- AgentScope 官方文档：[doc.agentscope.io](https://doc.agentscope.io/)
- AgentScope `v2.0.8` 源码标签：[v2.0.8](https://github.com/agentscope-ai/agentscope/tree/v2.0.8)
- 2.0.8 Agent API 源码：[src/agentscope/agent/_agent.py](https://github.com/agentscope-ai/agentscope/blob/v2.0.8/src/agentscope/agent/_agent.py)
- 2.0.8 消息模型源码：[src/agentscope/message/_base.py](https://github.com/agentscope-ai/agentscope/blob/v2.0.8/src/agentscope/message/_base.py)
- PyPI 发布页（版本元数据与安装包）：[agentscope 2.0.8](https://pypi.org/project/agentscope/2.0.8/)

### Provider 扩展包

本轮不需要额外 provider 扩展包。`agentscope==2.0.8` 的安装依赖已经包含其内置 provider 所需的基础包，包括 OpenAI、Anthropic 和 DashScope 路径。后续如果使用 OpenAI-compatible endpoint，优先评估 SDK 内置的 `OpenAIChatModel`；只有选择 AgentScope 的可选 provider（例如 Gemini、Ollama、XAI）时，才按官方 extras/依赖说明增加对应扩展。probe 本身不导入 provider client，也不读取任何 API key。

## 可复现实验

```bash
cd /Users/wangjiaquan/project/QAgent/backend
uv lock --check
uv run pytest app/tests/test_agentscope_compat.py -q
uv run ruff check scripts/agentscope_compat_probe.py app/tests/test_agentscope_compat.py
uv run python scripts/agentscope_compat_probe.py
```

`backend/scripts/agentscope_compat_probe.py` 是人工/诊断友好的独立 probe：它使用一个本地 `ChatModelBase` fake，实现四种模式——普通文本、结构化工具调用、非法结构化 JSON、provider 异常——并逐项打印检查结果。不会访问网络，不会读取或打印 API key，不会产生付费模型调用；任一检查失败时以非零退出码结束。

`backend/app/tests/test_agentscope_compat.py` 是 CI 使用的 pytest 自动化契约测试。它复用同一个本地 fake provider，但以独立测试断言锁定版本、消息和单轮 reply、文本提取、`structured_schema`、缺少 model、非法结构化 JSON 的有限重试以及 `ReActConfig.max_iters`。测试不依赖 probe 的命令行输出，也不访问网络或真实 provider。

CI 应在 `backend/` 目录执行上面的四条命令；其中 pytest 是自动化门禁，probe 用于输出便于人工排查的逐项诊断结果，`uv lock --check` 和 ruff 分别校验锁文件一致性与这两个验证入口的静态质量。

验证结果（2026-09-13）：

```text
AgentScope compatibility probe (expected 2.0.8)
offline fake provider: no network, no API key, no paid model call
PASS import and exact version: ok
PASS exception exports: ok
PASS Msg creation: ok
PASS single Agent / single turn text: ok
PASS structured JSON output: ok
PASS invalid Msg validation: ok
PASS invalid structured_schema validation: ok
PASS missing model error boundary: ok
PASS provider execution error boundary: ok
PASS bounded invalid structured JSON behavior: ok
PASS dict react_config error boundary: ok

Summary: 11/11 checks passed
```

## 已验证的实际 API

### Import 与版本

```python
import agentscope

assert agentscope.__version__ == "2.0.8"
```

### 输入：QAgent prompt / plan / run context → `Msg`

`Msg.content` 在 2.0.8 中必须是 block 列表；最小文本输入如下：

```python
from agentscope.message import Msg

message = Msg(
    name="user",
    role="user",
    content=[{"type": "text", "text": prompt}],
)
```

传入 `content="hello"` 会在构造/校验时抛 `pydantic_core.ValidationError`（具体类为 Pydantic 的 `ValidationError`）。因此未来 Runtime 应先把 QAgent 的 prompt、plan 和 run context 序列化/拼接为字符串，再放入一个 text block；不要直接把普通字符串作为 `content`。

### 调用：Agent 初始化与单 turn

2.0.8 的统一 Agent 构造方式是：

```python
from agentscope.agent import Agent

agent = Agent(
    name="qagent-run",
    system_prompt=system_prompt,
    model=model,  # agentscope.model.ChatModelBase 实例
)

output = await agent.reply(message)
```

实测签名为：

```text
Agent(
    name: str,
    system_prompt: str,
    model: ChatModelBase,
    toolkit=None,
    middlewares=None,
    state=None,
    offloader=None,
    model_config=None,
    context_config=None,
    react_config=None,
    injection_config=None,
)
```

`Agent` 构造器对 `model=None` 这类错误配置可能不会立即报错；真正执行 `reply()` 时会因缺少 `model.formatter` 抛 `AttributeError`。因此 Runtime 应在构造前完成 model/provider 配置校验，不能把 Agent 构造成功当作可执行性证明。

如需限制推理/动作迭代，使用配置对象，不要传普通 dict：

```python
from agentscope.agent import ReActConfig

react_config = ReActConfig(
    max_iters=50,
    structured_output_grace_iters=5,
    stop_on_reject=False,
)
agent = Agent(..., react_config=react_config)
```

传入 `react_config={"max_iters": 1}` 可能在构造时通过，但会在 `reply()` 阶段以 `AttributeError` 暴露错误。

### 输出：文本与结构化 JSON

普通文本返回一个 `agentscope.message.Msg`。文本应从其 block 内容中提取，而不是假设返回值是字符串：

```python
text = "".join(
    block.text
    for block in output.content
    if block.type == "text"
)
```

结构化输出使用 Pydantic model 作为 schema：

```python
from pydantic import BaseModel

class Result(BaseModel):
    answer: str
    score: int

output = await agent.reply(message, structured_schema=Result)
structured = output.structured_output
# {"answer": "...", "score": 7}
```

2.0.8 内部以 `GenerateStructuredOutput` 工具调用承载结构化结果；Runtime 应读取 `output.structured_output`，不要解析“结构化输出已生成”之类的最终文本。传入 `structured_schema=int` 会在 `reply()` 阶段抛 Pydantic `ValidationError`。

### 错误边界

本轮 fake provider 实测结论：

| 场景 | 实际表现 | 后续 Runtime 建议 |
| --- | --- | --- |
| `Msg` block/schema 不合法 | `pydantic_core.ValidationError` | 在输入边界捕获并转为可记录的配置/输入错误 |
| `Agent(model=None)` 或不兼容对象 | 构造可能成功；`reply()` 触发 `AttributeError`（例如缺少 `formatter`） | 构造前校验 model；执行边界仍捕获配置错误 |
| fake provider 抛 `RuntimeError` | 从 `await agent.reply()` 直接传播 | 捕获 provider/执行异常，写入 Runtime 错误事件 |
| 非法结构化 JSON | SDK 重试；在 `ReActConfig(max_iters=1, structured_output_grace_iters=1)` 下返回终止 `Msg`，`finished_reason == "exceed_max_iters"`，`structured_output is None` | 设置有限的 `max_iters`；检查 `finished_reason`、`error`、`structured_output`，不要无限重试 |
| 结构化工具相关异常 | SDK 暴露 `StructuredOutputError`、`ToolJSONDecodeError` 等异常类型 | 按 SDK 版本导入并作为细分异常；同时保留返回 `Msg` 状态字段检查 |

SDK 异常模块中已确认存在的相关类型包括：`agentscope.exception.StructuredOutputError`、`ToolJSONDecodeError`、`ToolInterruptedError`、`ToolNotFoundError`、`ToolGroupInactiveError`。具体 provider 的 HTTP、认证和限流异常仍应在真实 provider adapter 的边界统一包装；本轮不使用真实 key，因此不对 provider 网络异常做付费调用验证。

## 给未来 Runtime 的精确接入结论

### 输入

```text
QAgent 的 prompt / plan / run context
→ 序列化为一个明确的 prompt 字符串
→ Msg(
     name="user",
     role="user",
     content=[{"type": "text", "text": prompt}],
   )
```

建议在 Runtime 层保留可审计的 prompt 组装结果，并避免把凭证、API key 或 secret 放入 message、日志和结构化结果。

### 调用

```python
agent = Agent(
    name=run_agent_name,
    system_prompt=system_prompt,
    model=model,  # 由 provider/model 配置工厂创建 ChatModelBase
    react_config=ReActConfig(max_iters=50, structured_output_grace_iters=5),
)
output = await agent.reply(message, structured_schema=ResultModel)
```

这里的 `model` 必须是配置完成且带 formatter 的 `ChatModelBase` 实例；本轮只证明 fake model 可用，尚未实现 Runtime 的 model factory。

### 输出

- 文本：从返回 `Msg.content` 中筛选 `TextBlock` 并拼接。
- 结构化 JSON：从 `output.structured_output` 读取 dict；再由 Runtime 自己校验/映射到 QAgent Result / Asset / Event / Recovery Point DTO。
- 状态：同时记录 `finished_reason`、`error`、usage（如存在）和 provider request/correlation metadata（不得包含 secret）。

### 错误

建议至少划分以下边界：

1. 输入/schema 校验：捕获 Pydantic `ValidationError`。
2. Agent 配置：捕获 `AttributeError`、`ValueError` 等配置错误，并在调用前转换为明确的 Runtime 配置错误。
3. 执行/provider：捕获 `RuntimeError` 以及 provider SDK 的 HTTP、认证、限流和超时异常；以 Runtime 的统一错误事件/恢复点模型承接。
4. 结构化输出：关注 `StructuredOutputError` / `ToolJSONDecodeError`，并检查返回 Msg 的 `finished_reason`, `error`, `structured_output is None`。
5. 循环边界：显式设置 `ReActConfig.max_iters`，避免非法结构化输出导致无界重试。

### 配置

未来建议由部署环境注入以下变量，由独立 model/provider factory 消费；不要在 probe、代码、测试或文档中写入值：

```text
AGENTSCOPE_PROVIDER   # 例如 openai-compatible、anthropic、dashscope
AGENTSCOPE_MODEL      # provider 的模型名
AGENTSCOPE_ENDPOINT   # provider 或 OpenAI-compatible endpoint
AGENTSCOPE_API_KEY    # secret，仅从环境/secret manager 读取
AGENTSCOPE_TIMEOUT    # 请求超时，转换为 provider SDK 支持的 timeout 类型
```

变量名是 QAgent Runtime 的建议约定，不是 AgentScope 2.0.8 强制的环境变量名；具体 provider 构造仍须遵照 AgentScope 官方 provider 类签名和部署配置。

## 本轮边界

- 未修改 `backend/app/qagent_runtime/agentscope_adapter.py` 或其他 Runtime 主链。
- 未实现 Approved Run → AgentScope → Result / Asset / Event / Recovery Point 回写链。
- 未修改 LangGraph、SQLite/PostgreSQL schema、审批状态机、设备协议或 Gateway 主链。
- 未使用真实生产 API key，未发起真实网络模型请求，未产生付费调用。
- 本验证仅证明 AgentScope 2.0.8 的依赖、离线 fake provider API 和错误边界可由 CI 重复验证；不代表 AgentScope 已接入 QAgent Runtime，也不代表 Runtime 已具备真实 provider 的 model factory 或生产调用能力。
