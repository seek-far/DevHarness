"""
GitLabProvider — implements SourceProvider, VCSProvider, ReviewProvider
by delegating to the existing Repo class in services/gitlab_utils.py.

This preserves all existing GitLab behavior (proxy, SSH, env-specific logic).
"""

from __future__ import annotations
import asyncio
import json
import logging
import time
from pathlib import Path

import requests
import redis.asyncio as aioredis

from .base import SourceProvider, VCSProvider, ReviewProvider

import sys
sys.path.append(str(Path(__file__).resolve().parents[2]))
from settings import worker_cfg as cfg
from services.gitlab_utils import Repo

logger = logging.getLogger(__name__)


class GitLabProvider(SourceProvider, VCSProvider, ReviewProvider):
    """Full GitLab integration — existing behavior wrapped behind provider ABCs."""

    def __init__(self, project_web_url: str = ""):
        self._project_web_url = project_web_url
        self._repo_ready: dict[str, Path] = {}  # bug_id → repo_path (idempotent)

    # ── SourceProvider ────────────────────────────────────────────────────────

    def fetch_trace(self, *, project_id: str = "", job_id: str = "", **kwargs) -> str:
        url = f"{cfg.gitlab_api}/projects/{project_id}/jobs/{job_id}/trace"
        headers = {"PRIVATE-TOKEN": cfg.gitlab_private_token}

        req_kwargs: dict = {"headers": headers}
        if cfg.env == "local_ts_host__aca":
            req_kwargs["proxies"] = {
                "http":  f"socks5://{cfg.socks5_proxy}",
                "https": f"socks5://{cfg.socks5_proxy}",
            }

        logger.info("fetching trace project=%s job=%s", project_id, job_id)
        resp = requests.get(url, **req_kwargs)
        resp.raise_for_status()
        logger.info("trace fetched (%d chars)", len(resp.text))
        return resp.text

    def fetch_file(self, file_path: str, ref: str = "main") -> str:
        repo_path = Path(cfg.repo_base_path) / "_tmp_fetch"
        repo = Repo(repo_path=str(repo_path), repo_url=self._project_web_url)
        return repo.gitlab_fetch_file(file_path, branch=ref)

    # ── VCSProvider ───────────────────────────────────────────────────────────

    def ensure_repo_ready(self, bug_id: str) -> Path:
        # Idempotent: only clone once per bug_id
        if bug_id in self._repo_ready:
            return self._repo_ready[bug_id]
        repo_path = Path(cfg.repo_base_path) / bug_id
        repo = Repo(repo_path=str(repo_path), repo_url=self._project_web_url)
        repo.ensure_repo_ready()
        self._repo_ready[bug_id] = repo_path
        return repo_path

    def create_fix_branch(self, bug_id: str, repo_path: Path) -> dict:
        repo = Repo(repo_path=str(repo_path), repo_url=self._project_web_url)
        # ensure_repo_ready already called by the node; just do base branch + create
        repo.ensure_base_branch()
        import datetime
        now = datetime.datetime.now()
        branch_id = now.strftime("%H_%M_%S") + f"_{now.microsecond // 100000}"
        branch_name = f"auto/bug_{bug_id}-patch_{branch_id}"
        repo.run("checkout", "-b", branch_name)
        current_commit = repo.run("rev-parse", "HEAD")
        logger.info("created branch %s", branch_name)
        return {
            "status": "success",
            "branch_name": branch_name,
            "base_branch": "main",
            "commit": current_commit,
        }

    def commit_and_push(self, repo_path: Path, message: str) -> dict:
        repo = Repo(repo_path=str(repo_path), repo_url=self._project_web_url)
        return repo.commit_changes(message=message)

    # ── ReviewProvider ────────────────────────────────────────────────────────

    def create_review(self, repo_path: Path, state: dict) -> dict:
        repo = Repo(repo_path=str(repo_path), repo_url=self._project_web_url)
        return repo.gitlab_create_merge_request(
            source_branch=state["fix_branch_name"],
            title=f"[auto-fix] bug {state['bug_id']}",
            description=(
                f"Automatically generated fix for bug `{state['bug_id']}`.\n\n"
                f"**Error:**\n```\n{state.get('error_info', '')}\n```"
            ),
        )

    def wait_ci_result(self, bug_id: str, timeout: int = 300) -> str | None:
        return asyncio.run(_gitlab_wait_ci(bug_id, timeout))


# ── CI wait helper (moved from nodes/wait_ci_result.py) ──────────────────────

_TERMINAL = {"success", "failed"}
_TRANSIENT = {"pending", "running", "canceled", "skipped", "created",
              "waiting_for_resource", "preparing"}


async def _gitlab_wait_ci(bug_id: str, timeout: int) -> str | None:
    inbox_stream = cfg.worker_inbox_stream_key.format(bug_id=bug_id)
    inbox_group = cfg.worker_inbox_group
    consumer = cfg.worker_inbox_consumer
    block_ms = min(cfg.stream_block_ms, cfg.worker_heartbeat_interval * 1000)

    r = aioredis.from_url(cfg.redis_url, decode_responses=False)
    try:
        try:
            await r.xgroup_create(inbox_stream, inbox_group, id="0", mkstream=True)
        except Exception as e:
            if "BUSYGROUP" not in str(e):
                raise

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                break
            actual_block = min(block_ms, remaining_ms)

            try:
                results = await r.xreadgroup(
                    groupname=inbox_group,
                    consumername=consumer,
                    streams={inbox_stream: ">"},
                    count=1,
                    block=actual_block,
                )
            except Exception as exc:
                logger.error("xreadgroup error: %s", exc)
                await asyncio.sleep(1)
                continue

            if not results:
                continue

            for _stream, entries in results:
                for entry_id, fields in entries:
                    raw: bytes = fields.get(b"data") or fields.get("data", b"")
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError as exc:
                        logger.error("malformed msg id=%s: %s", entry_id, exc)
                        await r.xack(inbox_stream, inbox_group, entry_id)
                        continue

                    await r.xack(inbox_stream, inbox_group, entry_id)
                    status = msg.get("object_attributes", {}).get("status", "")
                    logger.debug("ci msg status=%s", status)

                    if status in _TERMINAL:
                        return status
                    elif status not in _TRANSIENT:
                        logger.warning("unexpected ci status=%s", status)
        return None
    finally:
        await r.aclose()
