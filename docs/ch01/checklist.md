# Chapter 01 Checklist: LLM 终端多轮对话

## 使用规则

- 本清单在实现完成后执行。
- 每项必须有命令输出、自动化测试或人工操作结果作为证据。
- 未执行真实 API 验证时，本章不得标记为完成。
- 任一项失败时返回 `tasks.md` 修复，修复后重新执行受影响的检查。

## 1. Spec、Plan 与范围

- [x] 实现与 `docs/ch01/spec.md` 一致。
- [x] `docs/ch01/plan.md` 已创建且与当前实现一致。
- [ ] 用户已审核 `docs/ch01/plan.md`。
- [x] 项目名准确为 `mewcode-agent`。
- [x] Python 包名准确为 `mewcode_agent`。
- [x] 启动命令准确为 `mewcode-agent`。
- [x] 未实现持久化、斜杠命令、工具调用或其他范围外功能。

## 2. 项目与依赖

- [x] `.python-version` 指向 Python `3.11.9`。
- [x] `pyproject.toml` 由 `uv` 管理。
- [x] `uv.lock` 存在且与 `pyproject.toml` 同步。
- [x] 运行依赖包含 `openai`、`anthropic`、`textual` 和 `pyyaml`。
- [x] 开发依赖包含 `pytest` 和 `pytest-asyncio`。
- [x] `pyproject.toml` 中存在准确入口：

  ```toml
  [project.scripts]
  mewcode-agent = "mewcode_agent.cli:main"
  ```

- [x] `pyproject.toml` 中存在准确的 pytest 配置：

  ```toml
  [tool.pytest.ini_options]
  testpaths = ["tests"]
  pythonpath = ["src"]
  addopts = ["--basetemp=.pytest-tmp"]
  asyncio_mode = "auto"
  markers = ["integration: calls the real DeepSeek API"]
  ```

- [x] `uv sync` 执行成功。

## 3. YAML 配置

- [x] 根目录存在 `llm_providers.yaml`。
- [x] `default_provider` 准确为 `deepseek_openai`。
- [x] Provider 标识准确为 `deepseek_openai` 和 `deepseek_anthropic`。
- [x] OpenAI 兼容 Base URL 准确为 `https://api.deepseek.com`。
- [x] Anthropic 兼容 Base URL 准确为 `https://api.deepseek.com/anthropic`。
- [x] 两个 Provider 的模型准确为 `deepseek-v4-pro`。
- [x] 两个 Provider 的 `api_key_env` 准确为 `DEEPSEEK_API_KEY`。
- [x] 两个 Provider 的 `max_tokens` 准确为 `4096`。
- [x] YAML 中不存在真实 API Key。
- [x] 无效 YAML、缺失字段、错误类型、错误协议和错误默认 Provider 均被拒绝。

## 4. OpenAI 兼容适配器

- [x] 使用 `AsyncOpenAI`。
- [x] 请求发送完整且顺序正确的历史消息。
- [x] 请求启用流式输出。
- [x] `stream_chat` 只产出新增文本片段。
- [x] 空片段不会污染最终文本。
- [x] 完全空响应被识别为错误。
- [x] 鉴权、限流、网络、超时和流中断错误被转换为脱敏应用错误。

## 5. Anthropic 兼容适配器

- [x] 使用 `AsyncAnthropic`。
- [x] 请求发送完整且顺序正确的历史消息。
- [x] 请求启用流式输出。
- [x] `stream_chat` 只产出新增文本片段。
- [x] 空片段不会污染最终文本。
- [x] 完全空响应被识别为错误。
- [x] 鉴权、限流、网络、超时和流中断错误被转换为脱敏应用错误。

## 6. 多轮对话

- [x] `ChatMessage.role` 只允许 `user` 和 `assistant`。
- [x] `ConversationHistory` 保持消息时间顺序。
- [x] 第一轮 user 消息在请求前进入历史。
- [x] 第一轮完整 assistant 回复在流结束后进入历史。
- [x] 第二轮请求包含第一轮 user、第一轮 assistant 和第二轮 user 消息。
- [x] API 错误文字不会进入模型上下文。
- [x] 进程退出时不写入会话文件。

## 7. Textual 界面

- [x] 界面包含 `chat-log`、`status` 和 `prompt-input`。
- [x] 空白输入不会发起请求。
- [x] 用户消息提交后立即显示。
- [x] assistant 文本随流式片段增量显示。
- [x] 请求期间输入框被禁用。
- [x] 同一时间不能并发发送多个请求。
- [x] 请求成功后输入框恢复并获得焦点。
- [x] 请求失败后显示脱敏错误，输入框恢复并获得焦点。
- [x] Textual 无界面测试覆盖输入、流式显示和异常恢复。

## 8. 安全与错误处理

- [x] 缺少 `llm_providers.yaml` 时应用退出码为 `1`。
- [x] 缺少或清空 `DEEPSEEK_API_KEY` 时应用退出码为 `1`。
- [x] 错误输出不包含 API Key。
- [x] 日志和测试输出不包含请求头或密钥。
- [x] `.gitignore` 排除本地密钥文件和 Python 运行缓存。
- [x] API 失败后应用仍可继续接收输入。

## 9. 自动化验证

- [x] 以下编译检查通过：

  ```powershell
  uv run python -m compileall -q src tests integration_tests
  ```

- [x] 未设置真实密钥时，以下默认测试通过且不访问外网：

  ```powershell
  uv run pytest
  ```

- [ ] 设置有效 `DEEPSEEK_API_KEY` 后，以下真实 API 测试通过：

  ```powershell
  uv run pytest integration_tests
  ```

- [x] 未设置 `DEEPSEEK_API_KEY` 时，真实 API 测试使用 `pytest.skip` 明确跳过。

- [ ] 真实测试确认 `deepseek_openai` 返回非空流式文本。
- [ ] 真实测试确认 `deepseek_anthropic` 返回非空流式文本。

## 10. 人工验收

- [ ] 执行 `uv run mewcode-agent` 成功启动界面。
- [ ] 状态区显示 `deepseek_openai` 和 `deepseek-v4-pro`。
- [ ] 输入第一轮问题，回复以流式方式显示。
- [ ] 输入第二轮追问，模型能够使用第一轮上下文。
- [ ] 将 `default_provider` 改为 `deepseek_anthropic` 并重启。
- [ ] Anthropic 兼容适配器同样完成两轮流式对话。
- [ ] 将 `default_provider` 恢复为 `deepseek_openai`。
- [ ] `README.md` 中的安装、配置、启动和测试命令均可直接执行。

## 11. 完成条件

- [ ] `tasks.md` 中所有实现任务均已完成。
- [ ] 本清单所有项目均已通过。
- [x] 最终文件结构与 `spec.md` 一致。
- [ ] 本章不存在未记录的已知失败。
