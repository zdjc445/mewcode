"""Built-in command catalog and UI-independent handlers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from mewcode_agent.agent import AgentLoop
from mewcode_agent.commands.models import (
    CommandDomainError,
    CommandError,
    CommandInvocation,
    CommandMode,
    CommandSpec,
    CommandUI,
    CommandUsageError,
    ConfirmationRequest,
)
from mewcode_agent.commands.registry import CommandRegistry
from mewcode_agent.compaction import ContextCompactionError
from mewcode_agent.history import ConversationHistory
from mewcode_agent.notes import NotesError, NotesManager
from mewcode_agent.security import PermissionMode, SecurityPolicyEngine
from mewcode_agent.sessions import (
    SessionError,
    SessionManager,
    SessionRecovery,
)


REVIEW_DEFAULT_PROMPT = (
    "请审查当前工作区尚未提交的代码更改。只读取和分析，不修改文件。"
    "请按严重程度列出可复现的问题，并给出精确文件与行号；"
    "如果没有发现问题，请明确说明剩余测试风险。"
)
REVIEW_SCOPED_PREFIX = (
    "请审查以下用户指定范围内的代码。只读取和分析，不修改文件。\n"
    "用户指定范围（原文）：\n"
)
REVIEW_SCOPED_SUFFIX = (
    "\n请按严重程度列出可复现的问题，并给出精确文件与行号；"
    "如果没有发现问题，请明确说明剩余测试风险。"
)

_CATEGORY_TITLES = {
    "general": "常用",
    "workflow": "工作流",
    "context": "上下文",
    "sessions": "会话",
    "memory": "记忆",
    "security": "权限",
}


@dataclass(frozen=True, slots=True)
class PermissionCommandPaths:
    user: Path
    project: Path
    permanent: Path

    def __post_init__(self) -> None:
        for path in (self.user, self.project, self.permanent):
            if not isinstance(path, Path) or not path.is_absolute():
                raise ValueError("权限配置路径必须是绝对 Path")


@dataclass(frozen=True, slots=True)
class BuiltinCommandServices:
    agent_loop: AgentLoop
    history: ConversationHistory
    session_manager: SessionManager
    notes_manager: NotesManager
    security_policy: SecurityPolicyEngine
    provider_id: str
    model: str
    permission_paths: PermissionCommandPaths
    activate_restored_session: Callable[[SessionRecovery], None]
    activate_new_session: Callable[[], None]

    def __post_init__(self) -> None:
        if not isinstance(self.provider_id, str) or not self.provider_id:
            raise ValueError("provider_id 必须是非空字符串")
        if not isinstance(self.model, str) or not self.model:
            raise ValueError("model 必须是非空字符串")
        if not isinstance(self.permission_paths, PermissionCommandPaths):
            raise ValueError("permission_paths 类型无效")
        if not callable(self.activate_restored_session) or not callable(
            self.activate_new_session
        ):
            raise ValueError("session activator 必须可调用")


class _BuiltinHandlers:
    def __init__(
        self,
        registry: CommandRegistry,
        services: BuiltinCommandServices,
    ) -> None:
        self._registry = registry
        self._services = services

    async def help(
        self,
        invocation: CommandInvocation,
        ui: CommandUI,
    ) -> None:
        if invocation.arguments:
            target_name = invocation.arguments
            if target_name.startswith("/"):
                target_name = target_name[1:]
            if not target_name or " " in target_name or "\t" in target_name:
                raise CommandUsageError
            spec = self._registry.resolve(target_name)
            if spec is None or spec.hidden:
                await ui.show_system_message(
                    (
                        f"帮助中没有公开命令：/{target_name.lower()}。"
                        "输入 /help 查看可用命令。",
                    )
                )
                return
            aliases = (
                ", ".join(f"/{alias}" for alias in spec.aliases)
                if spec.aliases
                else "无"
            )
            hint = spec.argument_hint if spec.argument_hint else "无"
            await ui.show_system_message(
                (
                    f"命令：/{spec.name}",
                    f"说明：{spec.description}",
                    f"类型：{spec.execution_kind}",
                    f"用法：{spec.usage}",
                    f"别名：{aliases}",
                    f"参数提示：{hint}",
                )
            )
            return

        lines: list[str] = ["可用命令："]
        current_category: str | None = None
        for spec in self._registry.public_specs():
            if spec.category != current_category:
                current_category = spec.category
                lines.append(f"[{_CATEGORY_TITLES[spec.category]}]")
            aliases = (
                f"（别名：{', '.join('/' + item for item in spec.aliases)}）"
                if spec.aliases
                else ""
            )
            lines.append(
                f"{spec.usage} - {spec.description}{aliases}"
            )
        await ui.show_system_message(tuple(lines))

    async def compact(
        self,
        invocation: CommandInvocation,
        ui: CommandUI,
    ) -> None:
        _require_no_arguments(invocation)
        ui.refresh_status("正在压缩上下文")
        try:
            result = await self._services.agent_loop.compact_history(
                self._services.history
            )
        except asyncio.CancelledError:
            ui.refresh_status("已取消：context_compaction_cancelled")
            raise
        except ContextCompactionError as exc:
            raise CommandDomainError(exc.code, exc.message) from exc
        if not result.changed:
            ui.refresh_status("没有可压缩的历史")
            return
        reduction = result.estimate_before - result.estimate_after
        ui.refresh_status(
            "上下文压缩完成："
            f"generation={result.generation}，"
            f"覆盖消息={result.covered_history_end}，"
            f"估算减少={reduction}"
        )

    async def clear(
        self,
        invocation: CommandInvocation,
        ui: CommandUI,
    ) -> None:
        _require_no_arguments(invocation)
        try:
            await self._services.notes_manager.flush_before_session_switch()
            session_id = self._services.session_manager.start_new(
                activate=self._services.activate_new_session
            )
        except (SessionError, NotesError) as exc:
            raise CommandDomainError(exc.code, exc.message) from exc
        ui.clear_transcript()
        ui.refresh_status(f"新会话：{session_id}")

    async def mode(
        self,
        invocation: CommandInvocation,
        ui: CommandUI,
    ) -> None:
        if not invocation.arguments:
            await ui.show_system_message(
                (f"当前默认模式：{ui.get_default_mode()}",)
            )
            return
        if invocation.arguments not in ("plan", "execute"):
            raise CommandUsageError
        mode: CommandMode = invocation.arguments  # type: ignore[assignment]
        ui.set_default_mode(mode)
        ui.refresh_status("就绪")
        await ui.show_system_message((f"默认模式已切换为：{mode}",))

    async def sessions(
        self,
        invocation: CommandInvocation,
        ui: CommandUI,
    ) -> None:
        _require_no_arguments(invocation)
        try:
            metas = await asyncio.to_thread(
                self._services.session_manager.list_sessions
            )
        except SessionError as exc:
            raise CommandDomainError(exc.code, exc.message) from exc
        if not metas:
            await ui.show_system_message(("当前项目没有已保存会话",))
            ui.refresh_status("会话列表为空")
            return
        await ui.show_system_message(
            tuple(
                f"{meta.session_id} | {meta.updated_at} | "
                f"{meta.title} | {meta.summary}"
                for meta in metas
            )
        )
        ui.refresh_status(f"已列出 {len(metas)} 个会话")

    async def resume(
        self,
        invocation: CommandInvocation,
        ui: CommandUI,
    ) -> None:
        session_id = _require_session_id(invocation.arguments)
        try:
            await self._services.notes_manager.flush_before_session_switch()
            recovery = await self._services.session_manager.resume_async(
                session_id,
                activate=self._services.activate_restored_session,
            )
        except (SessionError, NotesError) as exc:
            raise CommandDomainError(exc.code, exc.message) from exc
        ui.clear_transcript()
        try:
            preparation = (
                await self._services.agent_loop.prepare_restored_history(
                    self._services.history
                )
            )
        except ContextCompactionError as exc:
            ui.refresh_status(
                f"会话已恢复；恢复上下文处理失败：{exc.code}"
            )
            return
        details = (
            f"消息={len(recovery.messages)}，"
            f"修复={'是' if recovery.repaired else '否'}，"
            f"诊断={len(recovery.diagnostics)}"
        )
        if preparation is not None and preparation.summary_changed:
            details += "，已执行恢复压缩"
        ui.refresh_status(f"会话已恢复：{details}")

    async def session(
        self,
        invocation: CommandInvocation,
        ui: CommandUI,
    ) -> None:
        parts = invocation.arguments.split(" ")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise CommandUsageError
        action, session_id_text = parts
        if action not in ("path", "delete"):
            raise CommandUsageError
        session_id = _require_session_id(session_id_text)
        manager = self._services.session_manager
        try:
            if action == "path":
                path = await asyncio.to_thread(
                    manager.session_path,
                    session_id,
                )
                await ui.show_system_message((str(path),))
                ui.refresh_status("已显示会话路径")
                return
            target = await asyncio.to_thread(
                manager.prepare_delete,
                session_id,
            )
            confirmed = await ui.request_confirmation(
                ConfirmationRequest(
                    "session.delete",
                    "删除会话（不可恢复）",
                    (
                        ("session ID", target.session_id),
                        ("标题", target.title),
                        ("路径", str(target.path)),
                    ),
                    True,
                )
            )
            if not confirmed:
                ui.refresh_status("已取消删除会话")
                return
            await asyncio.to_thread(manager.delete, target)
        except SessionError as exc:
            raise CommandDomainError(exc.code, exc.message) from exc
        await ui.show_system_message(
            (f"已删除会话 {target.session_id}：{target.path}",)
        )
        ui.refresh_status("会话已删除")

    async def memory(
        self,
        invocation: CommandInvocation,
        ui: CommandUI,
    ) -> None:
        manager = self._services.notes_manager
        if not invocation.arguments:
            snapshot = manager.snapshot
            lines: list[str] = []
            for title, entries in (
                ("用户偏好", snapshot.user_preferences),
                ("纠正反馈", snapshot.correction_feedback),
                ("项目知识", snapshot.project_knowledge),
                ("参考资料", snapshot.references),
            ):
                lines.append(title)
                lines.extend(f"- {entry}" for entry in entries)
                if not entries:
                    lines.append("(空)")
            await ui.show_system_message(tuple(lines))
            ui.refresh_status("已显示当前笔记")
            return
        if invocation.arguments == "paths":
            await ui.show_system_message(
                (
                    f"user: {manager.paths.user}",
                    f"project: {manager.paths.project}",
                )
            )
            ui.refresh_status("已显示笔记路径")
            return
        scope_by_arguments = {
            "clear user": "user",
            "clear project": "project",
        }
        scope = scope_by_arguments.get(invocation.arguments)
        if scope is None:
            raise CommandUsageError
        try:
            target = manager.clear_target(scope)  # type: ignore[arg-type]
            confirmed = await ui.request_confirmation(
                ConfirmationRequest(
                    "notes.clear",
                    "清空笔记",
                    (("scope", target.scope), ("路径", str(target.path))),
                    True,
                )
            )
            if not confirmed:
                ui.refresh_status("已取消清空笔记")
                return
            await manager.clear(scope)  # type: ignore[arg-type]
        except NotesError as exc:
            raise CommandDomainError(exc.code, exc.message) from exc
        await ui.show_system_message(
            (f"已清空 {scope} 笔记：{target.path}",)
        )
        ui.refresh_status("笔记已清空")

    async def permissions(
        self,
        invocation: CommandInvocation,
        ui: CommandUI,
    ) -> None:
        policy = self._services.security_policy
        if invocation.arguments:
            if invocation.arguments == "reset":
                policy.set_mode_override(None)
            elif invocation.arguments in ("strict", "default", "permissive"):
                policy.set_mode_override(
                    invocation.arguments  # type: ignore[arg-type]
                )
            else:
                raise CommandUsageError
            ui.refresh_status("就绪")
        status = policy.status()
        paths = self._services.permission_paths
        await ui.show_system_message(
            (
                f"配置模式：{status.configured_mode}",
                f"当前有效模式：{status.effective_mode}",
                "进程内覆盖："
                f"{'是' if status.has_runtime_override else '否'}",
                "规则数量："
                f"user={status.user_rule_count}，"
                f"project={status.project_rule_count}，"
                f"permanent={status.permanent_rule_count}，"
                f"session={status.session_rule_count}",
                f"user: {paths.user}",
                f"project: {paths.project}",
                f"permanent: {paths.permanent}",
            )
        )

    async def status(
        self,
        invocation: CommandInvocation,
        ui: CommandUI,
    ) -> None:
        _require_no_arguments(invocation)
        try:
            context = self._services.agent_loop.context_status(
                self._services.history
            )
            policy = self._services.security_policy.status()
        except (ValueError, RuntimeError) as exc:
            raise CommandError("command_status_failed") from exc
        lines = [
            f"Provider：{self._services.provider_id}",
            f"Model：{self._services.model}",
            f"默认模式：{ui.get_default_mode()}",
            f"Session：{self._services.session_manager.active_session_id}",
            f"历史消息：{len(self._services.history.snapshot())}",
        ]
        if context is None:
            lines.append("Prompt Token：unavailable")
        else:
            lines.extend(
                (
                    "Prompt Token："
                    f"estimate={context.estimated_prompt_tokens}，"
                    f"calibrated={'是' if context.used_actual_baseline else '否'}，"
                    f"budget={context.prompt_budget_tokens}，"
                    f"auto_trigger={context.auto_trigger_tokens}",
                    "Checkpoint："
                    f"generation={context.checkpoint_generation}，"
                    f"覆盖消息={context.checkpoint_covered_messages}",
                    "自动压缩："
                    f"熔断={'是' if context.auto_compaction_disabled else '否'}，"
                    f"连续失败={context.consecutive_summary_failures}",
                )
            )
        notes = self._services.notes_manager
        lines.extend(
            (
                f"笔记：generation={notes.generation}，"
                f"未处理成功请求={notes.unprocessed_successes}",
                f"权限模式：{policy.effective_mode}",
                f"公开命令：{len(self._registry.public_specs())}",
            )
        )
        await ui.show_system_message(tuple(lines))

    async def review(
        self,
        invocation: CommandInvocation,
        ui: CommandUI,
    ) -> None:
        prompt = (
            REVIEW_DEFAULT_PROMPT
            if not invocation.arguments
            else REVIEW_SCOPED_PREFIX
            + invocation.arguments
            + REVIEW_SCOPED_SUFFIX
        )
        await ui.send_user_message(prompt, mode="execute")


def build_builtin_command_registry(
    services: BuiltinCommandServices,
) -> CommandRegistry:
    if not isinstance(services, BuiltinCommandServices):
        raise ValueError("services 类型无效")
    registry = CommandRegistry()
    handlers = _BuiltinHandlers(registry, services)
    specs = (
        CommandSpec(
            "help",
            ("h", "?"),
            "显示命令总览或单条命令帮助",
            "/help [command]",
            "local",
            "general",
            "公开命令名称或别名",
            handlers.help,
            status_hint=True,
        ),
        CommandSpec(
            "status",
            ("stat",),
            "显示模型、会话、Token、笔记和权限状态",
            "/status",
            "local",
            "general",
            "",
            handlers.status,
            status_hint=True,
        ),
        CommandSpec(
            "mode",
            (),
            "查看或切换后续普通消息的默认模式",
            "/mode [plan|execute]",
            "ui",
            "workflow",
            "精确小写 plan 或 execute",
            handlers.mode,
        ),
        CommandSpec(
            "review",
            ("code-review",),
            "让 Agent 只读审查代码",
            "/review [scope]",
            "agent",
            "workflow",
            "可选的用户指定审查范围原文",
            handlers.review,
        ),
        CommandSpec(
            "compact",
            ("compress",),
            "立即执行一次上下文压缩",
            "/compact",
            "ui",
            "context",
            "",
            handlers.compact,
            status_hint=True,
        ),
        CommandSpec(
            "clear",
            ("new",),
            "保留旧存档并切换到新的空会话",
            "/clear",
            "ui",
            "sessions",
            "",
            handlers.clear,
        ),
        CommandSpec(
            "sessions",
            (),
            "列出当前项目的已保存会话",
            "/sessions",
            "ui",
            "sessions",
            "",
            handlers.sessions,
        ),
        CommandSpec(
            "resume",
            (),
            "恢复当前项目的指定会话",
            "/resume <session_id>",
            "ui",
            "sessions",
            "32 位小写十六进制 session ID",
            handlers.resume,
        ),
        CommandSpec(
            "session",
            (),
            "显示会话路径或确认删除非活动会话",
            "/session <path|delete> <session_id>",
            "ui",
            "sessions",
            "精确小写子命令与 32 位小写十六进制 ID",
            handlers.session,
        ),
        CommandSpec(
            "memory",
            ("notes",),
            "查看、定位或确认清空分层笔记",
            "/memory [paths|clear user|clear project]",
            "ui",
            "memory",
            "精确小写子命令和 scope",
            handlers.memory,
        ),
        CommandSpec(
            "permissions",
            ("perms",),
            "查看权限状态或设置当前进程模式覆盖",
            "/permissions [strict|default|permissive|reset]",
            "ui",
            "security",
            "精确小写模式或 reset",
            handlers.permissions,
        ),
    )
    for spec in specs:
        registry.register(spec)
    if registry.status_hints() != ("/help", "/status", "/compact"):
        raise CommandError("command_registry_invalid")
    registry.freeze()
    return registry


def _require_no_arguments(invocation: CommandInvocation) -> None:
    if invocation.arguments:
        raise CommandUsageError


def _require_session_id(value: str) -> str:
    if len(value) != 32 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise CommandUsageError
    return value
