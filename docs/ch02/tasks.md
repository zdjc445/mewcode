# Chapter 02 Tasks: ReAct Agent 循环与事件流

## 文档状态

- 规格：已批准，见 `docs/ch02/spec.md`
- 实施计划：已编写，见 `docs/ch02/plan.md`
- 实现授权：尚未授权
- 默认执行方式：在当前工作区按 Task 顺序实施；每个 Task 独立执行 red → green → commit

## 任务清单

- [ ] Task 1：为六个内置工具加入 `read`、`write`、`command` 精确分类。
- [ ] Task 2：新增不可变 Agent 事件与一次性 `AgentRunContext`。
- [ ] Task 3：实现连续读并发、写/命令屏障、审批、拦截器与取消补齐的 `ToolScheduler`。
- [ ] Task 4：新增 `ThinkingBlock`，并只在 assistant 工具调用历史中保存 thinking 协议元数据。
- [ ] Task 5：把 OpenAI Provider 改为统一结构化事件流，并保存/回传 `reasoning_content`。
- [ ] Task 6：把 Anthropic Provider 改为统一结构化事件流，并保存/回传 thinking block 与 `signature`。
- [ ] Task 7：实现独立 `AgentLoop`、15 轮限制、120 秒 LLM 超时、plan-only 状态机和终止规则。
- [ ] Task 8：把 Textual 改为 Agent 事件消费者，加入 plan-only 开关、两类审批卡片和取消操作，并更新 CLI 组装。
- [ ] Task 9：运行全量离线回归、静态检查与文档验收，记录真实证据。

## 顺序约束

1. Task 2 依赖 Task 1 的 `ToolCategory`。
2. Task 3 依赖 Task 1 的 category 和 Task 2 的 Context/Event。
3. Task 5 依赖 Task 4 的 `ThinkingBlock`。
4. Task 6 依赖 Task 4 和 Task 5 的 Provider 统一事件。
5. Task 7 依赖 Task 1–6。
6. Task 8 依赖 Task 7。
7. Task 9 只能在 Task 1–8 的测试全部通过后执行。

## 每个 Task 的完成门槛

- 先运行该 Task 指定测试并观察预期失败。
- 只实现使该 Task 测试通过的范围，不引入规格外能力。
- 运行该 Task 指定测试和相关回归测试，exit code 必须为 `0`。
- 运行 `git diff --check`，exit code 必须为 `0`。
- 检查 diff 不包含 API Key、真实请求头、SDK 原始错误对象或无关文件。
- 使用 `docs/ch02/plan.md` 指定的 commit message 提交。

## 不属于本章的任务

- 复杂系统提示词模板或 Prompt 插件。
- 完整权限规则、危险命令识别、规则持久化。
- 子 Agent、Agent 递归工具调用或团队能力。
- 计划/会话持久化、上下文压缩、Token 预算或记忆系统。
- 强制终止已经进入 `asyncio.to_thread` 的线程。
- 未在 `docs/ch02/spec.md` 明确列出的后续能力。
