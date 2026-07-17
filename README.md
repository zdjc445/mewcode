# Mewcode Agent

一个使用 Textual 构建的终端 LLM Agent。当前版本通过 OpenAI 兼容协议或 Anthropic 兼容协议连接 DeepSeek，支持流式输出、进程内多轮对话和自动工具调用循环。

## 环境要求

- Python `3.11.9`
- `uv`
- 有效的 DeepSeek API Key

## 安装

```powershell
uv sync
```

## 配置 API Key

API Key 只通过环境变量提供，不写入 `llm_providers.yaml`：

```powershell
$env:DEEPSEEK_API_KEY = "你的 DeepSeek API Key"
```

环境变量只对当前 PowerShell 进程及其子进程生效。

## 启动

必须从项目根目录执行：

```powershell
uv run mewcode-agent
```

默认使用 `llm_providers.yaml` 中的 `deepseek_openai`。如需验证 Anthropic 兼容协议，将 `default_provider` 改为 `deepseek_anthropic` 后重启应用。

## 测试

默认测试不访问外网，也不需要 API Key：

```powershell
uv run pytest
```

真实 API 集成测试需要先设置 `DEEPSEEK_API_KEY`：

```powershell
uv run pytest integration_tests
```

编译检查：

```powershell
uv run python -m compileall -q src tests integration_tests
```

## 当前范围

- 支持流式响应。
- 支持当前进程内的多轮对话。
- 内置 `read_file`、`write_file`、`edit_file`、`run_command`、`find_files` 和 `search_code` 六个工具。
- 每次用户请求最多执行 10 个工具；工具结果会立即写入对话历史并回灌模型，直到模型返回最终文本。
- 模型在同一次响应中返回多个工具调用时，按响应索引顺序逐个执行。
- 达到 10 次工具调用上限后，应用会关闭工具并要求模型根据已有结果生成最终总结。
- 文件工具支持相对路径和绝对路径，不限制在项目目录内。
- 工具失败以结构化结果写入历史，不会导致应用退出。
- 不保存会话文件。
- 不包含斜杠命令、MCP 或上下文压缩。
