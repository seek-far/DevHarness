"""
Node: fetch_trace
Pulls the raw CI job trace text from GitLab and stores it in state.
"""

from __future__ import annotations
import logging
import requests
import os
import sys

from graph.state import BugFixState

from pathlib import Path
sys.path.append(str(Path.cwd()))
from settings import worker_cfg as cfg

logger = logging.getLogger(__name__)


def fetch_trace(state: BugFixState) -> BugFixState:
    project_id = state["project_id"]
    job_id = state["job_id"]

    url = f"{cfg.gitlab_api}/projects/{project_id}/jobs/{job_id}/trace"
    headers = {"PRIVATE-TOKEN": cfg.gitlab_private_token}

    kwargs: dict = {"headers": headers}
    if cfg.env == "local_ts_host__aca":
        kwargs["proxies"] = {
            "http":  f"socks5://{cfg.socks5_proxy}",
            "https": f"socks5://{cfg.socks5_proxy}",
        }

    logger.info("fetching trace project=%s job=%s", project_id, job_id)
    resp = requests.get(url, **kwargs)
    resp.raise_for_status()

    logger.info("trace fetched (%d chars)", len(resp.text))
    return {"trace": resp.text}
