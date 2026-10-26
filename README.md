# miniAgent

一个只做一件事的 Windows 本机 agent：

- 只支持 OpenAI 风格的 `chat/completions` 接口；
- 认证、base URL、模型参数都放在 `config.toml`；
- 只给模型一个工具 `pwsh`，用来执行 PowerShell；
- `loop.py` 循环调用模型，直到模型不再请求工具或达到 `max_turns`。

无第三方依赖，只需要 Python 3.11+（使用了标准库 `tomllib`）。

## 项目结构

| 文件 | 职责 |
| --- | --- |
| `agent.py` | 入口与组合根：参数解析、单次/交互模式、退出码 |
| `loop.py` | agent 循环：请求模型、执行工具、追加结果 |
| `durable.py` | 持久化接缝：`AgentContext` 协议与不落盘的 `LiveContext` |
| `events.py` | 事件日志与回放后端：JSONL 追加、崩溃恢复、分歧检测 |
| `demo_events.py` | 事件溯源演示：三种崩溃点，不需要 API key |
| `llm.py` | OpenAI 兼容客户端：请求、重试、响应解析 |
| `tools.py` | `Tool` / `ToolRegistry` 与内置 `pwsh` 工具 |
| `config.py` | 配置校验、shell 查找、运行时设置 |
| `prompt.py` | 默认系统提示词与运行时上下文 |
| `errors.py` | `ConfigError` / `AgentError` |
| `banner.py` | 可选的启动横幅（纯装饰层） |

`agent.py` 仍然是唯一入口，原有运行命令不变。

## 配置

编辑 `config.toml`：

```toml
[auth]
api_key = "sk-..."

[base]
url = "https://api.openai.com/v1"

[model]
name = "gpt-4o-mini"
temperature = 0.2
max_tokens = 4096
```

- `auth.api_key` 会作为 `Authorization: Bearer <key>` 发送。
- 也支持环境变量 `MINIAGENT_API_KEY`，优先级高于配置文件。
- base URL 一般填到 `/v1` 为止，程序会拼上 `/chat/completions`；也可以直接填完整的 `.../chat/completions`。
- 如果服务商/模型不接受 `temperature` 或 `max_tokens`，把对应行删掉即可。
- `config.toml` 已加入 `.gitignore`，避免误提交密钥。

其余可选项见 `config.toml` 内的注释：`agent.max_turns`、`agent.request_timeout`、`shell.executable`、`shell.timeout_seconds`、`shell.max_output_chars`。

## 运行

交互模式：

```powershell
python .\agent.py
```

单次任务：

```powershell
python .\agent.py "列出当前目录的 Python 文件，并告诉我哪个最值得先读"
```

指定配置和工作目录：

```powershell
python .\agent.py --config .\config.toml --cwd .\some-project "修一下测试"
```

交互模式启动时会打印一个 `AGENT` 横幅：青→紫逐字符渐变、圆角面板，约 0.3 秒的逐字显示动画。

横幅由独立的 `banner.py` 负责，是纯装饰层，不参与对话逻辑，任何异常都静默降级：

- 输出被重定向（非 TTY）时不打印；
- 终端宽度 < 47 列时去掉边框（只留艺术字），< 41 列时整体跳过；
- 设置 `NO_COLOR`、`TERM=dumb` 或终端不支持 ANSI 时退化为无色文本；
- `--no-banner` 完全关闭。

```powershell
python .\agent.py --no-banner
```

如果系统里的 `python` 不可用，换成任意 Python 3.11+ 解释器的完整路径即可。

交互模式下支持：

- `/clear`：清空对话上下文；
- `exit` 或 Ctrl+C：退出（任务执行中按 Ctrl+C 会中断当前任务）。

运行时 stderr 会打印 `[tool]` 和 `[api]` 状态，最终回答输出到 stdout。

## 工具行为

模型只能调用一个函数：

```json
{
  "name": "pwsh",
  "arguments": {
    "command": "Get-ChildItem",
    "timeout_seconds": 120
  }
}
```

工具通过 `tools.py` 里的 `ToolRegistry` 注册。启动时 `agent.py` 在读完配置后调用一次 `build_default_registry(config)`，把 `pwsh` 作为唯一的内置工具注册进去，再交给 `loop.run_agent()`：

- 工具定义（名称、描述、JSON Schema、handler）就在 `tools.py`；
- 注册动作集中在 `build_default_registry()`，不在 import 时发生——因为 schema 里要嵌入配置中的默认超时；
- 新增工具时只需定义 `Tool` 并在 `build_default_registry()` 里 `register()`，循环和入口都不用改；
- handler 返回字符串而不是抛异常，工具错误会作为 `error: ...` 交给模型自行修正。

每次调用都会启动一个新的 `pwsh -NoProfile -NonInteractive -Command ...` 进程：

- 当前目录取自启动参数 `--cwd`，默认为启动 `agent.py` 时所在的目录；
- 上一次调用里的变量、函数、`Set-Location` 不会保留；
- 需要在同一次调用中保持状态时，用 `;` 或换行把命令串起来；
- 超时会终止整个进程树，并把 `timed_out` 和已捕获的输出返回给模型；
- 输出过长时会保留头尾、截断中间。

如果找不到 `pwsh`，会尝试回退到 Windows PowerShell 5.1。建议安装 PowerShell 7。

## 循环逻辑

每个用户任务：

1. `loop.run_agent()` 把系统提示词和对话发给模型；
2. 模型若返回 `tool_calls`，通过 `ToolRegistry.execute()` 逐个执行，并把结果作为 `role=tool` 追加回对话；
3. 再次请求模型；
4. 模型不再调用工具时，打印回答，任务结束。

循环分成两层，边界就是未来换成持久化执行的那条线：

- `loop.run_task()` 只写控制流：无网络、无子进程、无时钟读取，所有副作用都经过 `durable.AgentContext`；
- `durable.AgentContext` 只有 `chat()` 和 `call_tool()` 两个方法，对应 Workflow 引擎里的 Activity；
- `durable.LiveContext` 是当前实现，直接调用 `llm.chat()` 和 `ToolRegistry.execute()`，不记录任何东西；
- `loop.run_agent()` 是给入口用的兼容包装，签名与之前一致。

要换成事件溯源或 Temporal，只需换掉 `AgentContext` 的实现，`run_task()` 一行都不用改。`agent.max_turns` 限制每个任务的模型往返次数，防止无限循环。agent 没有审批、沙箱或网络隔离。

## 事件日志与续跑

`--events` 把一次任务写成只追加的 JSONL 事件日志，`--resume` 从日志续跑：

```powershell
python .\agent.py --events .\.demo\run.jsonl "统计当前目录的 Python 文件数量"
python .\agent.py --events .\.demo\run.jsonl --resume
```

事件类型：

| 事件 | 写入时机 | 内容 |
| --- | --- | --- |
| `task_started` | 任务开始 | 初始 `messages` 与 `max_turns`。system 提示词含当前时间，必须记录而不是重建 |
| `chat_completed` | 模型响应后 | assistant 消息、`tool_calls`、`finish_reason`、请求指纹 |
| `tool_started` | 命令启动**之前** | 执行意图。崩溃后据此判断"可能已经执行" |
| `tool_completed` | 命令结束之后 | 返回给模型的文本 |
| `task_finished` | 任务结束 | 最终回答 |

回放不是读状态：`loop.run_task()` 会从头重跑，每个 `chat()` / `call_tool()` 都先从日志取结果，日志用完才真正调用。模型的请求（messages + tools）会算 sha256 指纹，对不上就报 `replay diverged`，而不是静默错位。

三种崩溃点对应三种结果：

| 崩溃位置 | 日志状态 | 恢复行为 |
| --- | --- | --- |
| 模型响应落盘后 | 有 `chat_completed`，无 `tool_started` | 正常续跑，工具只执行一次 |
| 命令启动前 | 有 `tool_started`，无 `tool_completed` | 默认 `abort`：结果未知，拒绝猜测 |
| 命令执行后、结果落盘前 | 同上 | 默认 `abort`；`--resume-policy retry` 会重跑，非幂等副作用重复 |

`--resume-policy retry` 是 at-least-once：能继续，但可能重复。默认 `abort` 是 at-most-once：不重复，但放弃已完成的工作。

演示脚本不需要 API key，用脚本化模型和一个记账工具，在子进程里真的 `os._exit`：

```powershell
python .\demo_events.py
```

它跑三个场景并打印 ledger 行数与日志体积，预期结果是 `1 / 0 / 2`——第三个场景正是"事件溯源解决不了副作用"的证据。

已知局限：

- 恢复的是上下文，不是现实。已删除的文件、已发出的请求不会被回滚；
- `--resume-policy retry` 会重复非幂等命令，这是语义决定的，不是 bug；
- 日志包含完整命令输出，可能夹带敏感信息，且只追加、无法删除；
- 回放成本随事件数线性增长，没有快照机制；
- 每个事件都 `fsync`，一次工具调用多两次同步写盘。

## 安全提示

**这是本机任意命令执行器。** 模型生成的 PowerShell 会直接以当前用户权限运行，可能读写/删除文件、访问网络或执行任意程序。请只在可信目录或隔离环境里使用，并在执行前留意 stderr 中的 `[tool]` 命令预览。

当前版本有意保持最小：

- 非流式输出；
- 单工具；
- 持久化是可选的单次任务事件日志，默认不写盘，交互模式不记录；
- 无命令审批、沙箱、MCP 或多模型路由。

## 测试

测试使用标准库 `unittest`，无需第三方依赖：

```powershell
python -m unittest discover -s tests -t . -v
```

测试会在 `tests/.tmp/` 下创建临时文件，该目录已加入 `.gitignore`。

## 许可证
本项目基于 [MIT License](LICENSE) 开源。
