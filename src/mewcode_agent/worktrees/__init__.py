"""Managed Git worktree isolation."""

from mewcode_agent.worktrees.git import (
    GitCommandResult,
    GitRepositoryIdentity,
    GitRunner,
    read_linked_worktree_head,
)
from mewcode_agent.worktrees.loader import load_worktree_config
from mewcode_agent.worktrees.initializer import WorktreeInitializer
from mewcode_agent.worktrees.manager import WorktreeManager
from mewcode_agent.worktrees.models import (
    WorktreeCloseResult,
    WorktreeConfigError,
    WorktreeCreateResult,
    WorktreeError,
    WorktreeInitializationDiagnostic,
    WorktreeRecord,
    WorktreeRuntimeConfig,
    WorktreeState,
    WorktreeStatus,
    WorktreeSwitchResult,
    managed_worktree_path,
    validate_object_id,
    validate_relative_config_path,
    validate_task_id,
    validate_worktree_name,
    worktree_branch_name,
)
from mewcode_agent.worktrees.storage import (
    load_worktree_state,
    read_worktree_state_main_root,
    state_data,
    worktree_state_lock,
    write_worktree_state,
)

__all__ = [
    "GitCommandResult",
    "GitRepositoryIdentity",
    "GitRunner",
    "WorktreeCloseResult",
    "WorktreeConfigError",
    "WorktreeCreateResult",
    "WorktreeError",
    "WorktreeInitializationDiagnostic",
    "WorktreeInitializer",
    "WorktreeManager",
    "WorktreeRecord",
    "WorktreeRuntimeConfig",
    "WorktreeState",
    "WorktreeStatus",
    "WorktreeSwitchResult",
    "load_worktree_config",
    "load_worktree_state",
    "managed_worktree_path",
    "read_worktree_state_main_root",
    "read_linked_worktree_head",
    "state_data",
    "validate_object_id",
    "validate_relative_config_path",
    "validate_task_id",
    "validate_worktree_name",
    "worktree_branch_name",
    "worktree_state_lock",
    "write_worktree_state",
]
