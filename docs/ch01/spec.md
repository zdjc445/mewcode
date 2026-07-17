# Chapter 01 Specification: LLM 终端多轮对话

## 1. 文档状态

- 状态：代码与模拟测试已完成，等待真实 API 验证
- 实现授权：用户已明确回复“开始实现”
- 本章目标：使用 Python 调通 DeepSeek LLM API，提供 Textual 终端界面，支持流式输出和进程内多轮对话。

## 2. 已确认的技术决策

| 项目 | 决策 |
| --- | --- |
| 项目名 | `mewcode-agent` |
| Python 包名 | `mewcode_agent` |
| Python 版本 | `3.11.9` |
| 依赖管理 | `uv` |
| 启动命令 | `mewcode-agent` |
| 终端框架 | `Textual` |
| 配置文件 | 项目根目录 `llm_providers.yaml` |
| 实际模型服务 | DeepSeek |
| 模型 | `deepseek-v4-pro` |
| API Key 环境变量 | `DEEPSEEK_API_KEY` |
| 默认协议适配器 | OpenAI 兼容协议 |
| 输出方式 | 流式输出 |
| 对话上下文 | 仅保存在当前进程内存中 |
| 单次最大输出 Token | `4096` |
| 自动化测试 | 默认运行模拟测试；真实 DeepSeek API 测试单独手动运行 |

## 3. 术语约定

- “模型服务”指实际处理请求的 DeepSeek API。
- “协议适配器”指应用使用的 SDK 和请求格式。
- `deepseek_openai` 指通过 OpenAI Python SDK 访问 DeepSeek 的 OpenAI 兼容接口。
- `deepseek_anthropic` 指通过 Anthropic Python SDK 访问 DeepSeek 的 Anthropic 兼容接口。
- 本章不直接访问 OpenAI 官方模型服务或 Anthropic 官方模型服务。

## 4. 项目范围

### 4.1 范围内

1. 使用 `uv` 初始化可安装的 Python 应用项目。
2. 从根目录 `llm_providers.yaml` 读取并校验 LLM 配置。
3. 从环境变量 `DEEPSEEK_API_KEY` 读取密钥。
4. 实现 OpenAI 兼容协议和 Anthropic 兼容协议两个适配器。
5. 两个适配器均连接 DeepSeek，并使用 `deepseek-v4-pro`。
6. 使用 Textual 提供消息输入、对话显示和状态显示。
7. 增量显示模型返回的流式文本。
8. 在内存中保存当前运行期间的 `user`、`assistant` 消息。
9. 每次请求携带当前会话的完整历史消息，实现多轮对话。
10. 提供不访问外网的默认自动化测试和单独执行的真实 API 集成测试。
11. 在 `README.md` 中记录安装、配置、启动和测试方法。

### 4.2 范围外

1. 对话记录持久化和历史会话恢复。
2. 历史消息裁剪、Token 计数和上下文压缩。
3. `/exit`、`/clear`、`/model` 等斜杠命令。
4. 运行期间切换协议适配器；切换方式是修改 `default_provider` 后重启应用。
5. 工具调用、文件读写、Shell 执行、MCP、RAG 和多 Agent。
6. OpenAI 官方 API 和 Anthropic 官方 API 的真实调用。
7. `.env` 文件加载、密钥录入界面和密钥持久化。
8. Markdown 富文本渲染、代码高亮和主题定制。
9. 并发发送多个用户请求。

## 5. 配置规范

根目录 `llm_providers.yaml` 必须使用以下准确结构：

```yaml
default_provider: deepseek_openai

providers:
  deepseek_openai:
    protocol: openai
    base_url: https://api.deepseek.com
    api_key_env: DEEPSEEK_API_KEY
    model: deepseek-v4-pro
    max_tokens: 4096

  deepseek_anthropic:
    protocol: anthropic
    base_url: https://api.deepseek.com/anthropic
    api_key_env: DEEPSEEK_API_KEY
    model: deepseek-v4-pro
    max_tokens: 4096
```

字段规则：

| YAML 路径 | 类型 | 规则 |
| --- | --- | --- |
| `default_provider` | 字符串 | 必须对应 `providers` 中存在的键 |
| `providers` | 映射 | 必须包含 `deepseek_openai` 和 `deepseek_anthropic` |
| `providers.*.protocol` | 字符串 | 只允许 `openai` 或 `anthropic` |
| `providers.*.base_url` | 字符串 | 必须使用上方列出的准确 HTTPS 地址 |
| `providers.*.api_key_env` | 字符串 | 必须为 `DEEPSEEK_API_KEY` |
| `providers.*.model` | 字符串 | 必须为 `deepseek-v4-pro` |
| `providers.*.max_tokens` | 正整数 | 必须为 `4096` |

配置文件只保存环境变量名，不保存 API Key 值。使用 `yaml.safe_load` 解析配置。缺少文件、缺少字段、字段类型错误、协议不受支持或默认适配器不存在时，应用必须给出明确错误并以退出码 `1` 结束。

## 6. 功能需求

### FR-01：应用启动

1. 用户在项目根目录执行 `uv run mewcode-agent`。
2. 应用读取当前工作目录下的 `llm_providers.yaml`。
3. 应用读取 `default_provider` 对应的配置。
4. 应用从 `DEEPSEEK_API_KEY` 读取密钥。
5. 配置和密钥有效后启动 Textual 界面。
6. 启动时显示当前适配器标识和模型标识，不显示密钥。

### FR-02：终端界面

Textual 应用类命名为 `ChatApp`，界面包含：

- `RichLog(id="chat-log", wrap=True, markup=False)`：显示当前会话。
- `Static(id="status")`：显示适配器、模型、请求状态或错误。
- `Input(id="prompt-input")`：接收用户输入。

空字符串或只包含空白字符的输入不得发起 API 请求。请求进行期间禁用输入框，保证同一时刻只有一个请求。请求结束或失败后重新启用输入框并恢复焦点。

### FR-03：流式输出

1. OpenAI 适配器使用 `AsyncOpenAI` 和 `client.chat.completions.create(..., stream=True)`。
2. Anthropic 适配器使用 `AsyncAnthropic` 和 `client.messages.stream(...)`。
3. 适配器统一返回 `AsyncIterator[str]`，每个元素只包含新增文本片段。
4. UI 累加文本片段，并持续重绘当前 assistant 回复。
5. 流完成后，将完整 assistant 回复加入内存历史。
6. 流没有产生任何非空文本时按请求失败处理。

### FR-04：多轮对话

消息模型命名为 `ChatMessage`，只允许以下角色：

```text
user
assistant
```

每条消息包含：

```text
role
content
```

会话历史由 `ConversationHistory` 按时间顺序保存。用户提交消息后将其加入历史；调用适配器时传入完整历史；流式响应成功完成后将完整 assistant 消息加入历史。API 错误文字只显示在 UI 中，不加入模型上下文。

### FR-05：适配器接口

统一接口命名为 `LLMProvider`，暴露以下异步方法：

```python
async def stream_chat(
    self,
    messages: list[ChatMessage],
) -> AsyncIterator[str]:
    ...
```

实现类：

- `OpenAIProvider`
- `AnthropicProvider`

工厂函数命名为 `create_provider`，根据 `ProviderConfig.protocol` 创建实现。UI 不得直接依赖 OpenAI 或 Anthropic SDK 的响应对象。

Provider 层对外只抛出 `ProviderError`。配置层对外只抛出 `ConfigError`。两类异常的消息都必须脱敏。

### FR-06：错误处理

必须处理并向用户显示以下错误类别：

- 配置文件不存在或 YAML 无法解析；
- 配置结构或字段值无效；
- `DEEPSEEK_API_KEY` 缺失或为空；
- API 鉴权失败；
- API 限流；
- 网络连接失败或超时；
- 流式响应中断；
- 空响应。

错误消息不得包含 API Key、完整请求头或 SDK 对象的原始敏感内容。

## 7. 代码结构

```text
.
├── docs/
│   └── ch01/
│       ├── spec.md
│       ├── plan.md
│       ├── tasks.md
│       └── checklist.md
├── integration_tests/
│   └── test_deepseek_streaming.py
├── src/
│   └── mewcode_agent/
│       ├── __init__.py
│       ├── __main__.py
│       ├── app.py
│       ├── cli.py
│       ├── config.py
│       ├── history.py
│       ├── models.py
│       └── providers/
│           ├── __init__.py
│           ├── anthropic_provider.py
│           ├── base.py
│           ├── factory.py
│           └── openai_provider.py
├── tests/
│   ├── conftest.py
│   ├── test_anthropic_provider.py
│   ├── test_app.py
│   ├── test_config.py
│   ├── test_history.py
│   ├── test_openai_provider.py
│   └── test_provider_factory.py
├── .gitignore
├── .python-version
├── llm_providers.yaml
├── pyproject.toml
├── README.md
└── uv.lock
```

职责划分：

| 文件 | 职责 |
| --- | --- |
| `config.py` | `ProviderConfig`、`AppConfig` 和 `load_config` |
| `models.py` | `ChatMessage` 与角色类型 |
| `history.py` | `ConversationHistory` |
| `providers/base.py` | `LLMProvider` 接口 |
| `providers/factory.py` | `create_provider` |
| `providers/openai_provider.py` | OpenAI 兼容协议转换和流读取 |
| `providers/anthropic_provider.py` | Anthropic 兼容协议转换和流读取 |
| `app.py` | `ChatApp`、界面状态和请求工作流 |
| `cli.py` | 配置加载、依赖组装、启动和退出码 |
| `__main__.py` | 支持 `python -m mewcode_agent` |

`pyproject.toml` 中的控制台入口必须为：

```toml
[project.scripts]
mewcode-agent = "mewcode_agent.cli:main"
```

pytest 配置必须为：

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src"]
addopts = ["--basetemp=.pytest-tmp"]
asyncio_mode = "auto"
markers = ["integration: calls the real DeepSeek API"]
```

## 8. 测试策略

### 8.1 默认测试

`uv run pytest` 只收集 `tests/`，不得访问外网，且不得要求真实 API Key。测试通过模拟 SDK 流验证：

- 配置解析和校验；
- 环境变量读取；
- 历史消息顺序；
- 两个适配器的消息转换；
- 流式文本片段输出；
- Provider 工厂选择；
- Textual 输入、禁用、增量显示和恢复焦点；
- API 错误不进入模型上下文。

### 8.2 真实 API 集成测试

`uv run pytest integration_tests` 必须显式执行。未设置 `DEEPSEEK_API_KEY` 时，集成测试使用 `pytest.skip` 明确跳过。设置密钥后，测试分别通过 `deepseek_openai` 和 `deepseek_anthropic` 发起最小流式请求，断言两个适配器均返回非空文本。测试日志不得输出密钥。

## 9. 验收标准

1. `uv sync` 成功创建环境并安装锁定依赖。
2. `uv run python -m compileall -q src tests integration_tests` 成功。
3. 未设置 `DEEPSEEK_API_KEY` 时，`uv run pytest` 全部通过且不访问外网。
4. 设置有效 `DEEPSEEK_API_KEY` 后，`uv run pytest integration_tests` 验证两个协议适配器均可流式访问 DeepSeek。
5. `uv run mewcode-agent` 能启动 Textual 界面。
6. 第一轮消息的回复在生成过程中增量显示。
7. 第二轮请求包含第一轮的 user 和 assistant 消息。
8. API 请求期间不能重复提交消息。
9. 配置或 API 错误可读、可恢复，并且不泄露密钥。
10. 进程退出后不写入任何对话文件。
11. 实现没有加入范围外功能。

## 10. 参考依据

- DeepSeek API：<https://api-docs.deepseek.com/>
- Textual 测试：<https://textual.textualize.io/guide/testing/>
- uv 项目管理：<https://docs.astral.sh/uv/guides/projects/>
- pytest 配置：<https://docs.pytest.org/en/stable/reference/customize.html>
