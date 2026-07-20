"""Public instruction-loading API."""

from mewcode_agent.instructions.loader import (
    INSTRUCTION_FILE_BYTES,
    INSTRUCTION_MAX_INCLUDE_DEPTH,
    INSTRUCTION_TOTAL_BYTES,
    load_instruction_documents,
)
from mewcode_agent.instructions.models import (
    InstructionConfigError,
    InstructionDocument,
    InstructionErrorCode,
    InstructionLayer,
)

__all__ = [
    "INSTRUCTION_FILE_BYTES",
    "INSTRUCTION_MAX_INCLUDE_DEPTH",
    "INSTRUCTION_TOTAL_BYTES",
    "InstructionConfigError",
    "InstructionDocument",
    "InstructionErrorCode",
    "InstructionLayer",
    "load_instruction_documents",
]
