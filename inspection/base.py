"""
inspection.base — the standalone inspector contract.

Deliberately NOT the `Agent` ABC: `Agent` is `fix(BugInput)→FixOutput`,
bug-fix-specific. The inspector has its own minimal contract
`CodeTarget → InspectionReport`, its own package, no dependency on bf_worker.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any

SCHEMA_VERSION = "1"

# The project failure-class niche (plan §1). The inspector reports ONLY these
# classes — defects tests structurally miss — not generic lint/style.
DEFECT_CLASSES = (
    "contract-mismatch",            # cross-module/contract drift (F18/F19/parser-suspect class)
    "silent-empty-or-wrong-key",    # reads a non-existent key / silently empty result
    "blind-index-or-unchecked-write",  # index/state write with no precondition check
    "doc-impl-drift",               # docstring/comment contradicts the implementation
    "eval-or-state-contamination",  # shared state leaks across runs/cells/processes
    "other",
)

SEVERITIES = ("high", "medium", "low", "info")


@dataclass
class CodeFile:
    path: str       # display/repo-relative path
    content: str


@dataclass
class CodeTarget:
    """What to review. Phase 1: a set of files plus a free-text description."""

    files: list[CodeFile] = field(default_factory=list)
    description: str = ""

    def total_chars(self) -> int:
        return sum(len(f.content) for f in self.files)


@dataclass
class Finding:
    severity: str          # one of SEVERITIES
    defect_class: str      # one of DEFECT_CLASSES
    file: str
    line: int | None
    title: str
    rationale: str         # why this is a real defect tests would miss
    suggestion: str        # direction of fix — NOT a patch

    def normalized(self) -> "Finding":
        sev = self.severity if self.severity in SEVERITIES else "info"
        dc = self.defect_class if self.defect_class in DEFECT_CLASSES else "other"
        return Finding(sev, dc, self.file, self.line, self.title,
                        self.rationale, self.suggestion)


@dataclass
class InspectionReport:
    schema_version: str
    target_description: str
    model: str | None
    findings: list[Finding] = field(default_factory=list)
    summary: str = ""
    error: str | None = None      # set when the inspection itself failed

    @property
    def has_findings(self) -> bool:
        return bool(self.findings)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=str)


class Inspector(ABC):
    """Reviews a CodeTarget and returns structured findings."""

    name: str = "inspector"

    @abstractmethod
    def review(self, target: CodeTarget) -> InspectionReport:
        ...
