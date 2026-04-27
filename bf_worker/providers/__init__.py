"""
providers package — abstracts source code access, VCS operations, and review output.

Public API:
    SourceProvider, VCSProvider, ReviewProvider  — ABCs
    GitLabProvider       — full GitLab integration (existing behavior)
    LocalGitProvider     — local git repo mode
    LocalNoGitProvider   — local directory mode (no git required)
"""
from .base import SourceProvider, VCSProvider, ReviewProvider
from .gitlab_provider import GitLabProvider
from .local_provider import LocalGitProvider, LocalNoGitProvider

__all__ = [
    "SourceProvider",
    "VCSProvider",
    "ReviewProvider",
    "GitLabProvider",
    "LocalGitProvider",
    "LocalNoGitProvider",
]
