# Chapter 01 Plan: LLM 终端多轮对话

## 1. 文档状态

- 状态：待用户审核
- 输入：已批准的 `docs/ch01/spec.md`
- 任务文件：继续使用 `docs/ch01/tasks.md`
- 说明：本文档在基础实现完成后补充，用于准确记录当前架构；不追溯声明实现前已经审批。

## 2. 架构概览

项目采用六层结构：

1. **入口层**：`cli.py` 定位配置文件、加载配置、创建 Provider、组装会话历史并启动 Textual 应用。
2. **配置层**：`config.py` 严格解析根目录 `llm_providers.yaml`，校验固定字段和值，并从环境变量读取活动 Provider 的密钥。
3. **消息与会话层**：`models.py` 定义不可变消息；`history.py` 在当前进程内按顺序保存消息并提供历史快照。
4. **Provider 抽象层**：`providers/base.py` 定义协议无关的 `LLMProvider` Protocol 和统一的 `ProviderError`。
5. **协议适配层**：OpenAI、Anthropic 两个适配器分别封装官方异步 SDK，把统一消息转换为各自的请求参数，并统一产出新增文本片段。
6. **终端界面层**：`app.py` 接收用户输入、启动后台流式任务、持续刷新回复、更新状态并维护多轮历史。

依赖方向如下：

```text
cli
├── config
├── provider factory
│   ├── LLMProvider Protocol
│   ├── OpenAIProvider
│   └── AnthropicProvider
├── ConversationHistory
└── ChatApp
    ├── LLMProvider Protocol
    └── ConversationHistory
        └── ChatMessage
```

`ChatApp` 不导入 OpenAI 或 Anthropic SDK，也不读取 SDK 的响应对象。

## 3. 核心数据结构与接口

### 3.1 `ProviderConfig`

```python
@dataclass(frozen=True, slots=True)
class ProviderConfig:
    provider_id: str
    protocol: Literal["openai", "anthropic"]
    base_url: str
    api_key_env: str
    model: str
    max_tokens: int
```

职责：表示 `llm_providers.yaml` 中一个经过严格校验的 Provider。

### 3.2 `AppConfig`

```python
@dataclass(frozen=True, slots=True)
class AppConfig:
    default_provider: str
    providers: Mapping[str, ProviderConfig]
    api_key: str

    @property
    def active_provider(self) -> ProviderConfig: ...
```

职责：保存全部 Provider、默认 Provider 和从环境变量读取的活动密钥。`api_key` 使用 `repr=False`，避免对象表示意外显示密钥。

### 3.3 `ChatMessage`

```python
ChatRole = Literal["user", "assistant"]

@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: ChatRole
    content: str
```

职责：表示一条经过校验的用户或助手消息。消息创建后不可修改，`content` 必须为非空字符串。

### 3.4 `ConversationHistory`

```python
class ConversationHistory:
    def add_user(self, content: str) -> ChatMessage: ...
    def add_assistant(self, content: str) -> ChatMessage: ...
    def snapshot(self) -> list[ChatMessage]: ...
    def __len__(self) -> int: ...
```

职责：在当前进程内按时间顺序保存消息。`snapshot()` 返回新列表，调用方不能通过清空或追加返回值修改内部列表。

### 3.5 `LLMProvider`

```python
class LLMProvider(Protocol):
    def stream_chat(
        self,
        messages: list[ChatMessage],
    ) -> AsyncIterator[str]: ...
```

职责：定义 UI 唯一依赖的 LLM 流式接口。每次迭代只产出本次新增的文本片段。

### 3.6 错误边界

```python
class ConfigError(RuntimeError): ...
class ProviderError(RuntimeError): ...
```

- 配置层只向入口层抛出脱敏后的 `ConfigError`。
- 协议适配层把 SDK 异常转换成脱敏后的 `ProviderError`。
- `ChatApp` 只处理 `ProviderError`，不依赖具体 SDK 的异常类型。

## 4. 模块设计

### 4.1 `mewcode_agent.config`

**职责：**

- 使用 `yaml.safe_load` 解析 YAML。
- 校验顶层字段、Provider 标识、字段集合、字段类型和固定值。
- 校验 `default_provider` 存在。
- 从活动 Provider 的 `api_key_env` 指定的环境变量读取密钥。

**对外接口：**

```python
def load_config(
    path: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> AppConfig: ...
```

**依赖：** 标准库、PyYAML。

### 4.2 `mewcode_agent.models`

**职责：** 定义 `ChatRole` 和 `ChatMessage`，在消息边界校验角色与正文。

**依赖：** 标准库。

### 4.3 `mewcode_agent.history`

**职责：** 管理单个进程生命周期内的有序对话历史。

**依赖：** `models.ChatMessage`。

### 4.4 `mewcode_agent.providers.base`

**职责：** 定义 `LLMProvider` Protocol 和 `ProviderError`，隔离 UI 与 SDK。

**依赖：** `models.ChatMessage`。

### 4.5 `mewcode_agent.providers.factory`

**职责：** 根据 `ProviderConfig.protocol` 创建 `OpenAIProvider` 或 `AnthropicProvider`。

**对外接口：**

```python
def create_provider(
    config: ProviderConfig,
    api_key: str,
) -> LLMProvider: ...
```

### 4.6 `mewcode_agent.providers.openai_provider`

**职责：**

- 使用 `AsyncOpenAI` 和配置的 `base_url` 创建客户端。
- 将 `ChatMessage` 转换为 OpenAI Chat Completions 消息映射。
- 使用 `stream=True` 获取异步流。
- 从 `chunk.choices[0].delta.content` 提取文本并逐块 `yield`。
- 把鉴权、限流、超时、连接、HTTP、API、流中断和空响应转换为 `ProviderError`。

### 4.7 `mewcode_agent.providers.anthropic_provider`

**职责：**

- 使用 `AsyncAnthropic` 和配置的 `base_url` 创建客户端。
- 将 `ChatMessage` 转换为 Anthropic messages 入参。
- 使用 `client.messages.stream(...)` 获取异步流。
- 遍历 `stream.text_stream` 并逐块 `yield`。
- 把鉴权、限流、超时、连接、HTTP、API、流中断和空响应转换为 `ProviderError`。

### 4.8 `mewcode_agent.app`

**职责：**

- 组合 `RichLog`、`Static` 和 `Input`。
- 拒绝空白输入以及请求期间的重复提交。
- 把用户消息加入历史。
- 在 Textual Worker 中消费 `LLMProvider.stream_chat(...)`。
- 每收到一个片段就累加到 `active_response` 并重新渲染会话。
- 正常结束后把完整助手回复加入历史。
- 失败时只显示错误，不把错误加入历史。
- 在请求结束或失败后重新启用输入框并恢复焦点。

### 4.9 `mewcode_agent.cli`

**职责：**

- 从当前工作目录定位 `llm_providers.yaml`。
- 加载 `AppConfig` 并取得活动 Provider。
- 创建具体 Provider 和空的 `ConversationHistory`。
- 启动 `ChatApp`。
- 将启动期配置错误转换为退出码 `1`。

## 5. 模块交互

### 5.1 启动流程

```text
uv run mewcode-agent
    → cli.main()
    → Path.cwd() / "llm_providers.yaml"
    → load_config(path)
        → yaml.safe_load(...)
        → 严格校验两个 Provider
        → 读取 DEEPSEEK_API_KEY
    → config.active_provider
    → create_provider(provider_config, api_key)
    → ConversationHistory()
    → ChatApp(...).run()
```

启动期发生 `ConfigError` 或 `ProviderError` 时，CLI 输出脱敏错误并返回 `1`，不启动 TUI。

### 5.2 一轮对话

```text
用户按 Enter
    → ChatApp.submit_prompt(...)
    → 去除输入首尾空白并拒绝空输入
    → history.add_user(prompt)
    → 禁用输入框
    → 状态改为“生成中”
    → 启动 stream_response Worker
    → provider.stream_chat(history.snapshot())
    → 每个文本片段追加到 active_response
    → 持续重绘 RichLog
    → 流正常结束
    → history.add_assistant(active_response)
    → 状态恢复“就绪”
    → 重新启用输入框并恢复焦点
```

第二轮请求调用 `history.snapshot()` 时包含第一轮的用户消息和完整助手消息。

### 5.3 请求错误

```text
SDK 异常或空响应
    → Provider 适配器转换为 ProviderError
    → ChatApp 捕获并显示脱敏错误
    → 丢弃未完成的 active_response
    → 不追加 assistant 错误消息
    → 重新启用输入框并恢复焦点
```

## 6. 双协议适配设计

| 设计点 | OpenAI 兼容协议 | Anthropic 兼容协议 |
| --- | --- | --- |
| SDK 客户端 | `AsyncOpenAI` | `AsyncAnthropic` |
| DeepSeek 地址 | `https://api.deepseek.com` | `https://api.deepseek.com/anthropic` |
| 流入口 | `chat.completions.create(..., stream=True)` | `messages.stream(...)` |
| 文本提取 | `chunk.choices[0].delta.content` | `stream.text_stream` |
| 上层输出 | `AsyncIterator[str]` | `AsyncIterator[str]` |
| 统一失败 | `ProviderError` | `ProviderError` |

两个适配器都在各自模块内完成消息映射和响应解析。项目不设置独立序列化模块；HTTP JSON 序列化由对应 SDK 完成。

## 7. TUI 并发设计

`stream_response` 使用：

```python
@work(exclusive=True, exit_on_error=False)
```

- Worker 让网络流消费与 Textual 事件处理并发进行，避免阻塞界面。
- `exclusive=True` 取消同组中尚未完成的旧 Worker，只保留最新任务。
- `exit_on_error=False` 防止 Worker 异常直接终止应用。
- 输入框在请求期间同时设置为禁用，正常用户操作不能提交第二个请求。
- `finally` 负责重新启用输入框并恢复焦点。

## 8. 文件组织

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
│   ├── test_cli.py
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

## 9. 技术决策

| 决策点 | 选择 | 理由 |
| --- | --- | --- |
| Provider 抽象 | `typing.Protocol` | 两个适配器只共享接口形状，不共享状态或实现；上层无需依赖具体父类。 |
| 流式返回值 | `AsyncIterator[str]` | 本章只显示正文文本；完成由迭代结束表示，失败由 `ProviderError` 表示。 |
| SDK 使用 | OpenAI、Anthropic 官方异步 SDK | SDK 负责 HTTP、鉴权和 SSE 协议细节，适配器只处理请求映射与文本提取。 |
| Provider 创建 | 工厂函数 `create_provider` | 把协议选择集中在一个位置，避免 CLI 和 UI 出现协议分支。 |
| 历史存储 | `ConversationHistory` 进程内列表 | 满足单会话多轮需求，退出时自然清空，不引入持久化。 |
| 历史暴露 | `snapshot()` 返回列表副本 | 防止适配器或 UI 修改内部列表结构。 |
| 密钥来源 | `DEEPSEEK_API_KEY` 环境变量 | YAML 只保存环境变量名，避免把密钥提交到项目文件。 |
| 配置策略 | 固定 Provider 和字段值的严格校验 | 保证本章只连接已确认的两个 DeepSeek 兼容端点。 |
| TUI 并发 | Textual Worker + 禁用输入框 | 保持 UI 响应，并保证单次只处理一个用户请求。 |
| 默认测试 | 模拟 SDK 流且不访问外网 | 测试稳定、可重复，不依赖密钥和外部服务。 |
| 真实验证 | 独立的 `integration_tests/` | 只有显式执行时才调用 DeepSeek，避免默认测试产生外部请求。 |

## 10. Spec 覆盖关系

| Spec 需求 | 设计归属 |
| --- | --- |
| FR-01 应用启动 | `config.py`、`factory.py`、`cli.py` |
| FR-02 终端界面 | `app.py` |
| FR-03 流式输出 | 两个 Provider、`app.py` |
| FR-04 多轮对话 | `models.py`、`history.py`、`app.py` |
| FR-05 适配器接口 | `providers/base.py`、`factory.py`、两个 Provider |
| FR-06 错误处理 | `config.py`、两个 Provider、`app.py`、`cli.py` |

## 11. 测试设计

### 11.1 默认自动化测试

执行：

```powershell
uv run pytest
```

`pyproject.toml` 将默认收集目录限制为 `tests/`。测试使用假客户端和模拟异步流覆盖：

- YAML 结构、字段和值校验；
- 密钥环境变量读取与错误；
- `ChatMessage` 和 `ConversationHistory`；
- Provider 工厂分派；
- 两个协议的请求消息映射、文本增量与错误转换；
- Textual 提交、流式刷新、历史追加和错误恢复；
- CLI 组装和启动错误退出码。

### 11.2 真实 API 集成测试

执行：

```powershell
uv run pytest integration_tests
```

- 未设置 `DEEPSEEK_API_KEY`：两项测试明确跳过。
- 设置有效 `DEEPSEEK_API_KEY`：分别通过 `deepseek_openai` 和 `deepseek_anthropic` 发起最小流式请求，并断言返回非空文本。
- 测试不得输出密钥。

### 11.3 验收状态

默认测试、编译和锁文件检查已经通过。真实 API 和人工 TUI 多轮验证仍以 `docs/ch01/checklist.md` 中未勾选项目为准。

