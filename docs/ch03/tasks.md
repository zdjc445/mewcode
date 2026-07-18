# Chapter 03 Tasks: 模块化 Prompt 与缓存可观测性

## 文档状态

- 规格：已批准，见 `docs/ch03/spec.md`
- 实施计划：已编写，见 `docs/ch03/plan.md`
- 实现授权：用户已授权落地实现
- 当前阶段：等待选择执行方式，业务代码尚未开始修改

## 任务清单

- [ ] Task 1：建立不可变 Prompt 数据模型、9 个中文内置静态模块和运行时正文。
- [ ] Task 2：实现用户全局与项目两层 Prompt YAML 的严格加载、精确覆盖、禁用、保护和排序。
- [ ] Task 3：实现会话环境、请求环境、Git 三态、异步参数化 Git 命令和固定 JSON。
- [ ] Task 4：实现 `PromptRuntime` 显式生命周期、追加式控制时间线、`PromptComposer` 和安全标签渲染。
- [ ] Task 5：建立 `ProviderRequest`、统一 usage 结果/事件及可选 `UsageCollector` 契约。
- [ ] Task 6：迁移 OpenAI Provider，按时间线降低控制消息并解析精确缓存 usage 字段。
- [ ] Task 7：迁移 Anthropic Provider，按确定规则合并控制块并映射真实确认的 usage 字段。
- [ ] Task 8：把 Prompt 生命周期和 usage 消费接入 `AgentLoop`，移除硬编码 Prompt 与计划批准伪 user 历史。
- [ ] Task 9：完成 CLI 启动组装、工具描述双重关键规则、TUI 隔离回归和 README 配置说明。
- [ ] Task 10：完成无网络全量回归、真实双 Provider 缓存报告、人工行为评估和最终验收。

## 顺序约束

1. Task 2 和 Task 3 依赖 Task 1 的类型；两者彼此独立。
2. Task 4 依赖 Task 1、Task 3，并消费 Task 1 的内置运行时正文。
3. Task 5 依赖 Task 1 的 `PromptItem`，不依赖具体 Provider。
4. Task 6 和 Task 7 依赖 Task 4 的标签渲染与 Task 5 的 Provider 契约。
5. Task 8 依赖 Task 4–7。
6. Task 9 依赖 Task 2–4 和 Task 8。
7. Task 10 只能在 Task 1–9 的离线测试全部通过后执行。

## 每个 Task 的完成门槛

- 先运行该 Task 指定测试并观察预期失败。
- 只实现该 Task 明确列出的能力，不加入热更新、工具发现、持久化或其他范围外功能。
- 运行该 Task 指定测试及相关回归，exit code 必须为 `0`。
- 运行 `git diff --check`，exit code 必须为 `0`。
- 检查错误、日志、报告和 staged diff 不包含 API Key、完整 Prompt、用户正文、模型正文、thinking 或工具参数。
- 使用 `docs/ch03/plan.md` 中对应 Task 的 commit message 提交。
- 只有真实执行并取得证据后，才勾选本文件与 `checklist.md` 的对应项目。

## 不属于本章的任务

- Prompt 配置热更新、文件监听或自动重载。
- 外部工具发现、安装、动态注册或工具市场。
- Context 压缩、Token 预算、持久化或长期记忆。
- 子 Agent、团队编排或 Agent 递归工具调用。
- 通用权限规则语言或由 Prompt 授予代码层权限。
- 对 DeepSeek 内部工具定义缓存位置作未经实测的结论。
- 把缓存 usage 展示在日常 TUI。
