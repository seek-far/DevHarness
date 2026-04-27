"""
Abstract base classes for the provider abstraction layer.

Three concerns are separated:
  - SourceProvider: fetch CI traces and source file content
  - VCSProvider:    branch creation, patch workspace, commit/push
  - ReviewProvider: post-fix output (MR, CI wait, report generation)
"""

from __future__ import annotations
from abc import ABC, abstractmethod
from pathlib import Path


class SourceProvider(ABC):
    """Fetch CI traces and source file content."""

    @abstractmethod
    def fetch_trace(self, **kwargs) -> str:
        """Return the raw CI trace / test output as a string."""

    @abstractmethod
    def fetch_file(self, file_path: str, ref: str = "main") -> str:
        """Return the content of a source file."""


class VCSProvider(ABC):
    """Version control operations for the fix workflow."""

    @abstractmethod
    def ensure_repo_ready(self, bug_id: str) -> Path:
        """Prepare the working directory. Returns the repo/working path."""

    @abstractmethod
    def create_fix_branch(self, bug_id: str, repo_path: Path) -> dict:
        """Create a fix branch. Returns {branch_name, ...}."""

    @abstractmethod
    def commit_and_push(self, repo_path: Path, message: str) -> dict:
        """Commit (and optionally push) changes. Returns {status, ...}."""


class ReviewProvider(ABC):
    """Post-fix output: merge requests, CI wait, reports."""

    @abstractmethod
    def create_review(self, repo_path: Path, state: dict) -> dict:
        """Create a merge request or review artifact. Returns result dict."""

    @abstractmethod
    def wait_ci_result(self, bug_id: str, timeout: int = 300) -> str | None:
        """Wait for CI result. Returns status string or None on timeout."""
