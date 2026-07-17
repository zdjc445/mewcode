# Chapter 01 Tasks: LLM 终端多轮对话

## 使用规则

- 当前状态：代码与模拟测试已完成，等待真实 API 验证。
- 用户明确回复“开始实现”后才能执行实现任务。
- 执行时按编号顺序推进。
- 每完成一个可验证步骤，将对应的 `[ ]` 改为 `[x]`。
- 任务未通过对应验证时不得勾选。
- 实现中发现与 `spec.md` 或 `plan.md` 冲突的需求时停止编码，先更新对应文档并由用户确认。

## 0. Spec 模式文档审核

- [x] 用户审核 `docs/ch01/spec.md`。
- [ ] 用户审核 `docs/ch01/plan.md`。
- [x] 用户审核 `docs/ch01/tasks.md`。
- [x] 用户审核 `docs/ch01/checklist.md`。
- [x] 用户明确回复“开始实现”。

## 1. 初始化 Python 项目

- [x] 在项目根目录执行：

  ```powershell
  uv init --package --name mewcode-agent --python 3.11.9 --vcs none .
  ```

- [x] 确认生成 `.python-version`、`pyproject.toml`、`README.md` 和 `src/mewcode_agent/`。
- [x] 添加运行依赖：

  ```powershell
  uv add openai anthropic textual pyyaml
  ```

- [x] 添加开发依赖：

  ```powershell
  uv add --dev pytest pytest-asyncio
  ```

- [x] 在 `pyproject.toml` 中配置入口：

  ```toml
  [project.scripts]
  mewcode-agent = "mewcode_agent.cli:main"
  ```

- [x] 在 `pyproject.toml` 中写入准确的 pytest 配置：

  ```toml
  [tool.pytest.ini_options]
  testpaths = ["tests"]
  pythonpath = ["src"]
  addopts = ["--basetemp=.pytest-tmp"]
  asyncio_mode = "auto"
  markers = ["integration: calls the real DeepSeek API"]
  ```
- [x] 更新 `.gitignore`，排除 `.venv/`、Python 缓存、pytest 缓存和本地密钥文件。
- [x] 执行 `uv lock` 并确认生成 `uv.lock`。

## 2. 创建并校验 LLM 配置

- [x] 按 `spec.md` 的准确结构创建根目录 `llm_providers.yaml`。
- [x] 在 `src/mewcode_agent/config.py` 中实现 `ProviderConfig`。
- [x] 在 `src/mewcode_agent/config.py` 中实现 `AppConfig`。
- [x] 实现 `load_config`，使用 `yaml.safe_load`。
- [x] 严格校验所有必需字段、类型、枚举和值。
- [x] 校验 `default_provider` 引用存在的 Provider。
- [x] 从 `api_key_env` 指定的 `DEEPSEEK_API_KEY` 环境变量读取密钥。
- [x] 确保配置对象、错误和日志不会包含密钥值。
- [x] 为有效配置、缺失文件、无效 YAML、缺失字段、无效协议、无效默认 Provider 和缺失密钥编写测试。

## 3. 实现消息模型和内存历史

- [x] 在 `src/mewcode_agent/models.py` 中实现 `ChatMessage`。
- [x] 将角色限制为准确值 `user` 和 `assistant`。
- [x] 在 `src/mewcode_agent/history.py` 中实现 `ConversationHistory`。
- [x] 实现按顺序追加 user 消息和 assistant 消息。
- [x] 实现获取完整历史快照，避免适配器修改内部列表。
- [x] 编写历史顺序、内容和快照隔离测试。

## 4. 实现 Provider 抽象和工厂

- [x] 在 `providers/base.py` 中定义 `LLMProvider.stream_chat` 和脱敏异常 `ProviderError`。
- [x] 统一返回 `AsyncIterator[str]`，SDK 响应对象不得泄漏到 UI 层。
- [x] 在 `providers/factory.py` 中实现 `create_provider`。
- [x] `protocol: openai` 必须创建 `OpenAIProvider`。
- [x] `protocol: anthropic` 必须创建 `AnthropicProvider`。
- [x] 不支持的协议必须抛出明确配置错误。
- [x] 编写 Provider 工厂测试。

## 5. 实现两个 DeepSeek 协议适配器

### 5.1 OpenAI 兼容协议

- [x] 在 `openai_provider.py` 中使用 `AsyncOpenAI`。
- [x] 使用 `https://api.deepseek.com`、`deepseek-v4-pro` 和 `4096`。
- [x] 将 `ChatMessage` 按原顺序转换为 Chat Completions `messages`。
- [x] 使用 `client.chat.completions.create(..., stream=True)` 启用流式请求。
- [x] 只输出新增的非空文本片段。
- [x] 将 SDK 的鉴权、限流、连接、超时和流中断异常转换为应用错误。
- [x] 使用模拟流编写正常、空片段、空响应和异常测试。

### 5.2 Anthropic 兼容协议

- [x] 在 `anthropic_provider.py` 中使用 `AsyncAnthropic`。
- [x] 使用 `https://api.deepseek.com/anthropic`、`deepseek-v4-pro` 和 `4096`。
- [x] 将 `ChatMessage` 按原顺序转换为 Messages API `messages`。
- [x] 使用 `client.messages.stream(...)` 启用流式请求。
- [x] 只输出新增的非空文本片段。
- [x] 将 SDK 的鉴权、限流、连接、超时和流中断异常转换为应用错误。
- [x] 使用模拟流编写正常、空片段、空响应和异常测试。

## 6. 实现 Textual 终端界面

- [x] 在 `app.py` 中实现 `ChatApp`。
- [x] 创建 `RichLog(id="chat-log", wrap=True, markup=False)`。
- [x] 创建 `Static(id="status")`。
- [x] 创建 `Input(id="prompt-input")`。
- [x] 忽略空白输入。
- [x] 提交有效输入后加入 user 历史并立即显示。
- [x] 请求期间禁用输入框，并阻止并发提交。
- [x] 在后台异步执行 `stream_chat`，不得阻塞 Textual 事件循环。
- [x] 累加文本片段并持续重绘当前 assistant 回复。
- [x] 流成功结束后将完整 assistant 回复加入历史。
- [x] API 失败时显示脱敏错误，且错误文字不加入模型上下文。
- [x] 请求结束或失败后启用输入框并恢复焦点。
- [x] 使用 Textual `run_test()` 和 `Pilot` 编写无界面测试。

## 7. 实现 CLI 入口

- [x] 在 `cli.py` 中实现同步函数 `main`。
- [x] 从当前工作目录加载 `llm_providers.yaml`。
- [x] 创建默认 Provider、ConversationHistory 和 ChatApp。
- [x] 配置或密钥错误时输出脱敏消息并以退出码 `1` 结束。
- [x] 正常关闭时以退出码 `0` 结束。
- [x] 在 `__main__.py` 中调用 `main`，支持 `python -m mewcode_agent`。
- [x] 验证 `uv run mewcode-agent` 可以解析控制台入口。

## 8. 配置测试与真实 API 验证

- [x] 确保 `uv run pytest` 只执行 `tests/`，不访问网络。
- [x] 在 `integration_tests/test_deepseek_streaming.py` 中实现 OpenAI 兼容接口真实测试。
- [x] 在同一文件中实现 Anthropic 兼容接口真实测试。
- [x] 未设置 `DEEPSEEK_API_KEY` 时，集成测试必须使用 `pytest.skip` 明确跳过，不能使用占位密钥请求 API。
- [ ] 设置有效密钥后执行：

  ```powershell
  uv run pytest integration_tests
  ```

- [ ] 确认两个适配器均产生非空流式文本。
- [ ] 确认测试输出不包含 `DEEPSEEK_API_KEY` 的值。

## 9. 文档与最终验证

- [x] 更新 `README.md`，记录 Python 版本、`uv sync`、环境变量设置、启动命令和两类测试命令。
- [x] 执行：

  ```powershell
  uv sync
  uv run python -m compileall -q src tests integration_tests
  uv run pytest
  ```

- [ ] 使用真实 API 完成至少两轮终端对话。
- [x] 验证第二轮请求包含第一轮完整历史。
- [ ] 按 `docs/ch01/checklist.md` 逐项检查并勾选。
- [ ] 只有 Checklist 全部通过后，才将本章标记为完成。
