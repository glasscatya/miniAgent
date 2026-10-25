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

`agent.max_turns` 限制每个任务的模型往返次数，防止无限循环。agent 没有审批、沙箱或网络隔离。

## 安全提示

**这是本机任意命令执行器。** 模型生成的 PowerShell 会直接以当前用户权限运行，可能读写/删除文件、访问网络或执行任意程序。请只在可信目录或隔离环境里使用，并在执行前留意 stderr 中的 `[tool]` 命令预览。

当前版本有意保持最小：

- 非流式输出；
- 单工具；
- 无对话持久化；
- 无命令审批、沙箱、MCP 或多模型路由。

## 测试

测试使用标准库 `unittest`，无需第三方依赖：

```powershell
python -m unittest discover -s tests -t . -v
```

测试会在 `tests/.tmp/` 下创建临时文件，该目录已加入 `.gitignore`。

## 许可证
本项目基于 [MIT License](LICENSE) 开源。
