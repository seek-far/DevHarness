"""
inspection — standalone code review / inspection agent.

Independent of the bug-fix pipeline (no imports from bf_worker). Targets
defects that the bug-fix test oracle structurally cannot catch — found only
by reading code: cross-module contract mismatch, silent-empty / wrong-key
bugs, blind/unchecked writes, doc↔implementation drift, eval/state
contamination.

Phase 1: standalone only. See /mnt/d/PL/sdlcma/code-review-agent-plan.md.
"""

from inspection.base import (
    CodeFile,
    CodeTarget,
    DEFECT_CLASSES,
    Finding,
    InspectionReport,
    Inspector,
)

__all__ = [
    "CodeFile",
    "CodeTarget",
    "DEFECT_CLASSES",
    "Finding",
    "InspectionReport",
    "Inspector",
]
