"""Public context-compaction API."""

from mewcode_agent.compaction.artifacts import ContextArtifactStore
from mewcode_agent.compaction.estimator import ContextTokenEstimator
from mewcode_agent.compaction.manager import (
    CONTEXT_BOUNDARY_TEXT,
    ContextPreparation,
    ContextProjector,
    ContextWindowManager,
    ManualCompactionResult,
    history_atomic_boundaries,
)
from mewcode_agent.compaction.models import (
    ArtifactReference,
    CompactionConfig,
    ContextCompactionError,
    ContextEstimate,
    SummaryCheckpoint,
    SummarySections,
    ToolCompactionResult,
    VerbatimUserMessage,
)
from mewcode_agent.compaction.summarizer import (
    SUMMARY_SYSTEM_PROMPT,
    ContextSummarizer,
    SummaryGeneration,
)
from mewcode_agent.compaction.tool_results import ToolResultCompactor

__all__ = [
    "ArtifactReference",
    "CompactionConfig",
    "ContextArtifactStore",
    "ContextCompactionError",
    "ContextEstimate",
    "ContextTokenEstimator",
    "SummaryCheckpoint",
    "SUMMARY_SYSTEM_PROMPT",
    "ContextSummarizer",
    "CONTEXT_BOUNDARY_TEXT",
    "ContextProjector",
    "ContextPreparation",
    "ContextWindowManager",
    "SummaryGeneration",
    "ManualCompactionResult",
    "SummarySections",
    "ToolCompactionResult",
    "ToolResultCompactor",
    "VerbatimUserMessage",
    "history_atomic_boundaries",
]
