"""Best-effort environment initialization for managed worktrees."""

from __future__ import annotations

import os
from pathlib import Path
import shutil

from mewcode_agent.worktrees.git import GitRunner
from mewcode_agent.worktrees.models import (
    WorktreeInitializationDiagnostic,
    WorktreeRuntimeConfig,
)


class WorktreeInitializer:
    def __init__(
        self,
        *,
        main_root: Path,
        config: WorktreeRuntimeConfig,
        git: GitRunner,
    ) -> None:
        self._main_root = main_root.resolve(strict=True)
        self._config = config
        self._git = git

    async def initialize(
        self,
        worktree_root: Path,
    ) -> tuple[WorktreeInitializationDiagnostic, ...]:
        target_root = worktree_root.resolve(strict=True)
        diagnostics: list[WorktreeInitializationDiagnostic] = []
        for relative in self._config.local_config_files:
            diagnostic = self._copy_configured_path(
                relative,
                target_root,
                stage="local_config",
                failure_code="worktree_local_config_failed",
            )
            if diagnostic is not None:
                diagnostics.append(diagnostic)
        diagnostic = await self._configure_hooks(target_root)
        if diagnostic is not None:
            diagnostics.append(diagnostic)
        for relative in self._config.dependency_links:
            diagnostic = self._link_dependency(relative, target_root)
            if diagnostic is not None:
                diagnostics.append(diagnostic)
        for relative in self._config.copy_ignored:
            diagnostic = await self._copy_ignored(relative, target_root)
            if diagnostic is not None:
                diagnostics.append(diagnostic)
        return tuple(diagnostics)

    def _source(self, relative: str) -> Path | None:
        source = self._main_root.joinpath(*relative.split("/"))
        if not os.path.lexists(source):
            return None
        resolved = source.resolve(strict=True)
        resolved.relative_to(self._main_root)
        self._validate_source_tree(source, frozenset())
        return source

    def _validate_source_tree(
        self,
        source: Path,
        ancestors: frozenset[Path],
    ) -> None:
        resolved = source.resolve(strict=True)
        resolved.relative_to(self._main_root)
        if not source.is_dir():
            return
        if resolved in ancestors:
            raise ValueError("source symlink cycle")
        descendants = ancestors | {resolved}
        for child in source.iterdir():
            self._validate_source_tree(child, descendants)

    @staticmethod
    def _destination(target_root: Path, relative: str) -> Path:
        destination = target_root.joinpath(*relative.split("/"))
        destination.resolve(strict=False).relative_to(target_root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.parent.resolve(strict=True).relative_to(target_root)
        return destination

    def _copy_configured_path(
        self,
        relative: str,
        target_root: Path,
        *,
        stage: str,
        failure_code: str,
    ) -> WorktreeInitializationDiagnostic | None:
        try:
            source = self._source(relative)
            if source is None:
                return None
            destination = self._destination(target_root, relative)
            if os.path.lexists(destination):
                return None
            if source.is_dir():
                shutil.copytree(source, destination, symlinks=False)
            else:
                shutil.copy2(source, destination)
        except (OSError, RuntimeError, ValueError):
            return WorktreeInitializationDiagnostic(
                stage,
                relative,
                failure_code,
            )
        return None

    async def _configure_hooks(
        self,
        target_root: Path,
    ) -> WorktreeInitializationDiagnostic | None:
        try:
            configured = await self._git.run(
                self._main_root,
                "config",
                "--get",
                "core.hooksPath",
                check=False,
                error_code="worktree_hooks_failed",
            )
            if configured.returncode == 1:
                return None
            if configured.returncode != 0 or not configured.stdout:
                raise ValueError("core.hooksPath unavailable")
            raw_path = Path(configured.stdout)
            hooks_path = (
                raw_path
                if raw_path.is_absolute()
                else self._main_root / raw_path
            ).resolve(strict=False)
            await self._git.run(
                self._main_root,
                "config",
                "extensions.worktreeConfig",
                "true",
                error_code="worktree_hooks_failed",
            )
            await self._git.run(
                target_root,
                "config",
                "--worktree",
                "core.hooksPath",
                str(hooks_path),
                error_code="worktree_hooks_failed",
            )
        except (OSError, RuntimeError, ValueError):
            return WorktreeInitializationDiagnostic(
                "hooks",
                "core.hooksPath",
                "worktree_hooks_failed",
            )
        return None

    def _link_dependency(
        self,
        relative: str,
        target_root: Path,
    ) -> WorktreeInitializationDiagnostic | None:
        try:
            source = self._source(relative)
            if source is None or not source.is_dir():
                raise ValueError("dependency source is not a directory")
            destination = self._destination(target_root, relative)
            if os.path.lexists(destination):
                return None
            destination.symlink_to(
                source.resolve(strict=True),
                target_is_directory=True,
            )
        except (OSError, RuntimeError, ValueError):
            return WorktreeInitializationDiagnostic(
                "dependency_link",
                relative,
                "worktree_dependency_link_failed",
            )
        return None

    async def _copy_ignored(
        self,
        relative: str,
        target_root: Path,
    ) -> WorktreeInitializationDiagnostic | None:
        try:
            ignored = await self._git.run(
                self._main_root,
                "check-ignore",
                "-q",
                "--",
                relative,
                check=False,
                error_code="worktree_ignored_check_failed",
            )
        except (OSError, RuntimeError, ValueError):
            return WorktreeInitializationDiagnostic(
                "copy_ignored",
                relative,
                "worktree_ignored_check_failed",
            )
        if ignored.returncode == 1:
            return WorktreeInitializationDiagnostic(
                "copy_ignored",
                relative,
                "worktree_ignored_not_ignored",
            )
        if ignored.returncode != 0:
            return WorktreeInitializationDiagnostic(
                "copy_ignored",
                relative,
                "worktree_ignored_check_failed",
            )
        return self._copy_configured_path(
            relative,
            target_root,
            stage="copy_ignored",
            failure_code="worktree_ignored_copy_failed",
        )
