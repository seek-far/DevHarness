"""
Agent ABC and the shared BugInput / FixOutput contract.

This is the load-bearing seam between bug-fix approaches and the rest of the
system. Both running mode (standalone CLI, GitLab worker) and evaluation mode
construct a BugInput, hand it to one or more Agent implementations, and
consume FixOutput.

Keep these types deliberately minimal. Add fields when a concrete adapter or
metric needs them, not in anticipation.
"""

from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal


Outcome = Literal["fixed", "no_fix", "error", "already_fixed"]


@dataclass
class BugInput:
    """Inputs for a single bug-fix attempt.

    The provider object encapsulates source access, VCS operations, and review
    output. It is what makes one BugInput portable across modes (the same
    BugInput shape works for GitLab, local-git, local-no-git, and fixtures).
    """
    bug_id: str
    provider: Any  # SourceProvider & VCSProvider & ReviewProvider implementation
    project_id: str = ""
    project_web_url: str = ""
    job_id: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass
class FixOutput:
    """Outcome of a single bug-fix attempt.

    `final_state` carries the rich graph state (sanitized — no provider object)
    so evaluators can inspect telemetry like react_step_count, tool calls,
    confidence, etc., without coupling to any specific agent's internals.
    """
    outcome: Outcome
    bug_id: str
    error: str | None = None
    iterations: int = 0
    final_state: dict | None = None


class Agent(ABC):
    """Base class for all bug-fix approaches.

    `name` identifies the approach in journal entries and metric reports.
    Implementations must be importable and constructible without side effects;
    side effects belong in `fix()`.
    """

    name: str = "agent"

    @abstractmethod
    def fix(self, bug_input: BugInput) -> FixOutput:
        """Attempt to fix the bug described by bug_input."""
