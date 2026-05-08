"""
GitLabProvider — implements SourceProvider, VCSProvider, ReviewProvider for GitLab.

Owns the Repo helper that wraps git CLI + GitLab REST API, including env-specific
behavior (proxy, SSH host rewrites for docker-compose / tailscale).
"""

from __future__ import annotations
import asyncio
import base64
import datetime
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import time
from pathlib import Path

import requests
import redis.asyncio as aioredis

from .base import SourceProvider, VCSProvider, ReviewProvider

import sys
sys.path.append(str(Path(__file__).resolve().parents[2]))
from settings import worker_cfg as cfg

logger = logging.getLogger(__name__)


# ── Repo: git CLI + GitLab REST helpers ──────────────────────────────────────

def on_rm_error(func, path, exc_info):
    # Remove read-only attribute
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _to_ssh_url(repo_url):
    """
    Convert http://host/path  →  ssh://git@host:2222/path.git
      path = group/repo
    """
    m = re.match(r"https?://([^/]+)/(.+)$", repo_url)
    if not m:
        raise ValueError(f"Invalid repo_url format: {repo_url}")

    host, path = m.groups()

    ssh_port = cfg.gitlab_ssh_port #os.environ.get("GITLAB_SSH_PORT", "2222")

    # Non-port-22 connections must use the ssh:// syntax
    return f"ssh://git@{host}:{ssh_port}/{path}.git"


class Repo:
    def __init__(self, repo_path: str, repo_url: str):
        self.repo_path = Path(repo_path)
        if cfg.env == "local_multi_process":
            self.repo_url = repo_url.replace("gitlab.local","localhost:8080")
        elif cfg.env == 'local_docker_compose':
            self.repo_url = repo_url.replace("gitlab.local","gitlab")
            self.ssh_url = _to_ssh_url(self.repo_url)
        elif cfg.env == 'local_ts_host':
            if not "8080" in repo_url:
                self.repo_url = repo_url.replace("xxx.tailnnn.ts.net" , "xxx.tailnnn.ts.net:8080")
            self.ssh_url = _to_ssh_url(repo_url) #input to _to_ssh_url should not contain 8080
            logger.debug(f"self.ssh_url={self.ssh_url}")
        elif cfg.env == 'local_ts_host__aca':
            self.repo_url = repo_url.replace(cfg.gitlab_fqdn, cfg.gitlab_ip)
            self.ssh_url = _to_ssh_url(self.repo_url) #input to _to_ssh_url should not contain 8080
            if not "8080" in repo_url:
                self.repo_url = self.repo_url.replace(cfg.gitlab_ip , f"{cfg.gitlab_ip}:8080")
            logger.debug(f"self.repo_url={self.repo_url},self.ssh_url={self.ssh_url}")

        self.token = cfg.gitlab_private_token#os.environ["GITLAB_PRIVATE_TOKEN"]

    def run(self, *args, cwd=None):
        """Run a git command in target directory."""
        env = os.environ.copy()
        # Disable host key checking globally (simplest approach during the debug phase)
        env.setdefault(
            "GIT_SSH_COMMAND",
            "ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null",
        )

        args_list = list(args)
        if cfg.env == "local_ts_host__aca" :
            socks5_proxy = cfg.socks5_proxy
            if args_list[0]=="-c" and args_list[2]=="clone":
                args_list[1] += f' -o "ProxyCommand=nc -X 5 -x {socks5_proxy} %h %p"'
            elif args_list[0] in ["fetch", "push", "pull"]:
                #args_list[:0] = ["-c", f'core.sshCommand=ssh -T -p 2222 "ProxyCommand=nc -X 5 -x {socks5_proxy} %h %p"']
                args_list[:0] = ["-c", f'core.sshCommand=ssh -T -p 2222  -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ProxyCommand="nc -X 5 -x {socks5_proxy} %h %p"']

            logger.debug("args_list=======")
            for (i, arg) in enumerate(args_list):
                logger.debug(f"  {i}:{arg}")
        result = subprocess.run(
            #["git", *args],
            ["git"] + args_list,
            cwd=cwd or self.repo_path,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            logger.debug(f"Git error result={result}")
            raise RuntimeError(f"Git error: {result.stderr.strip()}")
        if True:
            logger.debug(f"args={args}\nstdout={result.stdout.strip()}")
        return result.stdout.strip()

    def ensure_origin_ssh(self):
        self.run("remote", "set-url", "origin", self.ssh_url)

    # ---------------------------
    # 🧩 1️⃣ Check/prepare repository
    # ---------------------------
    def ensure_repo_ready(self):
        if os.path.exists(self.repo_path):
            shutil.rmtree(self.repo_path, onerror=on_rm_error)
        """Ensure the directory is a valid Git repo; clone if needed."""
        if not (self.repo_path / ".git").exists():
            logger.debug(f"⚙️ No .git found in {self.repo_path}, cloning...")
            self.repo_path.mkdir(parents=True, exist_ok=True)
            # clone into the directory itself
            # if os.environ.get("ENV", "") != "local_ts_host":
            if cfg.env not in ["local_ts_host", "local_ts_host__aca", "local_docker_compose"]: #os.environ.get("ENV", "")
                auth_url = self.repo_url.replace("http://", f"http://{cfg.gitlab_username}:{self.token}@")
                self.run("clone", auth_url, ".", cwd=self.repo_path)
            elif cfg.env == "local_docker_compose":
                auth_url = self.repo_url.replace("http://", f"http://{cfg.gitlab_username}:{self.token}@")
                self.run("clone", auth_url, ".", cwd=self.repo_path)
                self.ensure_origin_ssh()
            else:
                self.run("-c", "core.sshCommand=ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null", "clone", self.ssh_url, ".", cwd=self.repo_path)
                self.ensure_origin_ssh()
            #self.run("clone", self.repo_url, ".", cwd=self.repo_path)
        else:
            if cfg.env in ["local_ts_host", "local_ts_host__aca"]: #os.environ.get("ENV", "")
                self.ensure_origin_ssh()
            logger.debug(f"✅ Found existing Git repo at {self.repo_path}")

    # https://chatgpt.com/g/g-p-68d6aea6596081918d694b543728ecef-job-hunting/c/6918f511-3368-8325-9eea-12fdc32b9a97
    def ensure_base_branch(self, base_branch="main"):
        """
        Ensure the base branch exists locally and is up-to-date.
        Handles:
          - local branch exists
          - only remote branch exists
          - fallback to master
          - fetch failure should NOT kill workflow
        """
        # ---------- STEP 1: Try safe fetch ----------
        try:
            self.run("fetch", "origin")
        except RuntimeError as e:
            logger.debug(f"⚠️ git fetch failed, but continuing: {e}")

        # ---------- STEP 2: Detect remote branches ----------
        remote_branches = self.run("branch", "-r")

        has_main_remote = f"origin/{base_branch}" in remote_branches
        has_master_remote = "origin/master" in remote_branches

        # ---------- STEP 3: Detect local branches ----------
        local_branches = self.run("branch")

        has_main_local = base_branch in local_branches

        # ---------- STEP 4: Checkout logic ----------
        if has_main_local:
            # Local main already exists — switch directly (don't create)
            self.run("checkout", base_branch)

        else:
            # No local main — create it from the remote
            if has_main_remote:
                self.run("checkout", "-b", base_branch, f"origin/{base_branch}")
            elif has_master_remote:
                logger.debug("🔁 Falling back to 'master'")
                self.run("checkout", "-b", base_branch, "origin/master")
            else:
                raise RuntimeError("❌ Neither main nor master branch found in remote.")

        # ---------- STEP 5: Try pull but tolerate failure ----------
        try:
            self.run("pull", "origin", base_branch)
        except RuntimeError as e:
            logger.debug(f"⚠️ git pull failed (ignored): {e}")

        logger.debug(f"✅ Base branch ready: {base_branch}")


    # ---------------------------
    # 🧩 3️⃣ Create fix branch (idempotent)
    # ---------------------------
    def deterministic_branch_name(self, bug_id: str, base_commit: str) -> str:
        """Stable branch name keyed on (bug_id, base_commit).

        Two runs of the same bug against the same base produce the same name —
        which lets create_fix_branch reuse an existing branch instead of
        forking a new one. If the user moves the base (rebase / new merge),
        the name changes and a fresh branch is born; that's the right
        semantic, not a bug.
        """
        return f"auto/bf/{bug_id}-{base_commit[:8]}"

    def create_fix_branch(self, bug_id=None, base_branch="main"):
        """Create or reuse a fix branch. Idempotent.

        Returns:
          status="success" — branch newly created locally
          status="reused"  — local branch already pointed at base_commit
        Caller should look up an existing remote MR separately (see
        find_open_or_merged_mr_for_branch) for full R10 short-circuit.
        """
        self.ensure_repo_ready()
        self.ensure_base_branch(base_branch)

        base_commit = self.run("rev-parse", "HEAD")
        branch_name = self.deterministic_branch_name(bug_id, base_commit)

        # Local branch existence: `git rev-parse --verify <branch>` exits 0 if it exists.
        local_exists = self._branch_exists_local(branch_name)
        if local_exists:
            self.run("checkout", branch_name)
            status = "reused"
            logger.debug(f"♻️ Reused local branch {branch_name} on base {base_commit[:8]}")
        else:
            self.run("checkout", "-b", branch_name)
            status = "success"
            logger.debug(f"✅ Created branch {branch_name} from {base_branch}")

        return {
            "status":      status,
            "branch_name": branch_name,
            "base_branch": base_branch,
            "commit":      base_commit,
        }

    def _branch_exists_local(self, branch_name: str) -> bool:
        try:
            self.run("rev-parse", "--verify", "--quiet", f"refs/heads/{branch_name}")
            return True
        except RuntimeError:
            return False

    def _branch_exists_remote(self, branch_name: str) -> bool:
        """Cheap remote check via REST: GET /projects/:id/repository/branches/:branch."""
        m = re.match(r"(https?://[^/]+)/(.+?)(?:\.git)?$", self.repo_url)
        if not m:
            return False
        host, project_path = m.groups()
        project_id = requests.utils.quote(project_path, safe="")
        url = f"{host}/api/v4/projects/{project_id}/repository/branches/{requests.utils.quote(branch_name, safe='')}"
        headers = {"PRIVATE-TOKEN": self.token} if self.token else {}
        try:
            if cfg.env != "local_ts_host__aca":
                resp = requests.get(url, headers=headers, timeout=15)
            else:
                proxies = {
                    "http":  f"socks5://{cfg.socks5_proxy}",
                    "https": f"socks5://{cfg.socks5_proxy}",
                }
                resp = requests.get(url, headers=headers, proxies=proxies, timeout=15)
        except requests.RequestException as exc:
            logger.warning("branch-exists check failed for %s: %s", branch_name, exc)
            # On lookup failure we assume "not exists" so the caller falls into
            # the regular create/push path; GitLab will surface a real error
            # (e.g. 409 on create) which is the authoritative answer.
            return False
        return resp.status_code == 200

    def find_merged_mr_by_bug_prefix(self, bug_id: str) -> dict | None:
        """Loose-match probe: any merged MR whose source_branch starts with
        `auto/bf/{bug_id}-` (the deterministic-name prefix).

        Used by precheck_already_fixed to short-circuit *before* clone+LLM.
        Bug_id is anchored with a trailing dash so `BUG-1` doesn't match
        `BUG-12`. Returns None on lookup failure (caller treats as "no MR
        found, run normally") — better to do the work than to spuriously
        skip a real bug.

        Semantically: if any earlier fix attempt for this bug merged at
        any base, the fix is in main now and a fresh run on a newer base
        should still short-circuit. That's the correct R10 semantic.
        """
        m = re.match(r"(https?://[^/]+)/(.+?)(?:\.git)?$", self.repo_url)
        if not m:
            return None
        host, project_path = m.groups()
        project_id = requests.utils.quote(project_path, safe="")
        url = f"{host}/api/v4/projects/{project_id}/merge_requests"
        params = {
            "source_branch_search": f"auto/bf/{bug_id}-",
            "state": "merged",
            "order_by": "updated_at",
        }
        headers = {"PRIVATE-TOKEN": self.token} if self.token else {}
        try:
            if cfg.env != "local_ts_host__aca":
                resp = requests.get(url, headers=headers, params=params, timeout=15)
            else:
                proxies = {
                    "http":  f"socks5://{cfg.socks5_proxy}",
                    "https": f"socks5://{cfg.socks5_proxy}",
                }
                resp = requests.get(url, headers=headers, params=params, proxies=proxies, timeout=15)
        except requests.RequestException as exc:
            logger.warning("merged-MR prefix probe failed for %s: %s", bug_id, exc)
            return None
        if resp.status_code != 200:
            return None
        items = resp.json() or []
        prefix = f"auto/bf/{bug_id}-"
        for mr in items:
            sb = mr.get("source_branch") or ""
            # Defense: GitLab's source_branch_search is substring; we want
            # prefix + dash anchoring to prevent BUG-1 matching BUG-12.
            if sb.startswith(prefix) and mr.get("state") == "merged":
                return self._mr_to_dict(mr)
        return None

    def find_open_or_merged_mr_for_branch(self, branch_name: str) -> dict | None:
        """Return the most recent open/merged MR for source_branch, if any.

        Open MRs win over merged MRs (we want the live review thread).
        Returns None when no MR exists or the lookup itself failed (treat as
        "no MR" — caller will create one and any duplicate would surface as
        an explicit conflict from GitLab).
        """
        m = re.match(r"(https?://[^/]+)/(.+?)(?:\.git)?$", self.repo_url)
        if not m:
            return None
        host, project_path = m.groups()
        project_id = requests.utils.quote(project_path, safe="")
        url = f"{host}/api/v4/projects/{project_id}/merge_requests"
        params = {"source_branch": branch_name, "state": "all", "order_by": "created_at"}
        headers = {"PRIVATE-TOKEN": self.token} if self.token else {}
        try:
            if cfg.env != "local_ts_host__aca":
                resp = requests.get(url, headers=headers, params=params, timeout=15)
            else:
                proxies = {
                    "http":  f"socks5://{cfg.socks5_proxy}",
                    "https": f"socks5://{cfg.socks5_proxy}",
                }
                resp = requests.get(url, headers=headers, params=params, proxies=proxies, timeout=15)
        except requests.RequestException as exc:
            logger.warning("MR lookup failed for %s: %s", branch_name, exc)
            return None
        if resp.status_code != 200:
            return None
        items = resp.json() or []
        # Prefer open, then merged, then any (closed) — newest-first per GitLab default.
        for state_filter in ("opened", "merged"):
            for mr in items:
                if mr.get("state") == state_filter:
                    return self._mr_to_dict(mr)
        return None

    @staticmethod
    def _mr_to_dict(mr: dict) -> dict:
        return {
            "id":    mr.get("id"),
            "iid":   mr.get("iid"),
            "title": mr.get("title"),
            "url":   mr.get("web_url"),
            "state": mr.get("state"),  # opened | merged | closed | locked
        }

    def commit_changes(self, message: str = "ci_agent: auto commit changes"):
        """Commit and push local changes. Three-state idempotent push.

        After staging+committing locally, compare the resulting tree to what
        already exists on the remote branch:
          - remote branch absent          → push, status="success"
          - remote head == base_commit    → push, status="success"  (first fix on this branch)
          - remote tree == our tree       → no push, status="reused" (same content already there)
          - remote tree != our tree       → force-push, status="updated" (overwrite stale fix)

        Equality is computed at the *tree* level (`commit^{tree}`), so commit
        SHAs differing for metadata reasons (timestamps, author) don't trip
        the "reused" path.
        """
        if not (self.repo_path / ".git").exists():
            raise RuntimeError(f"Not a git repo: {self.repo_path}")

        status = self.run("status", "--porcelain")
        if not status:
            logger.debug("ℹ️ No changes to commit.")
            return {"status": "no_changes"}

        logger.debug("🪶 Changes detected, committing locally...")
        self.run("add", "-A")
        try:
            self.run("commit", "-m", message)
        except RuntimeError as e:
            if "nothing to commit" in str(e):
                return {"status": "no_changes"}
            raise

        branch       = self.run("rev-parse", "--abbrev-ref", "HEAD")
        local_commit = self.run("rev-parse", "HEAD")
        local_tree   = self.run("rev-parse", "HEAD^{tree}")

        push_status = self._idempotent_push(branch, local_tree)

        logger.debug(f"✅ Push status={push_status} commit={local_commit[:8]} branch={branch}")
        return {
            "status": push_status,
            "branch": branch,
            "commit": local_commit,
        }

    def _idempotent_push(self, branch: str, local_tree: str) -> str:
        """Push local HEAD to origin/<branch> with content-aware idempotency.

        Decision tree:
          remote branch absent                                       → push, "success"
          remote tree == local tree                                  → no-op, "reused"
          remote is ancestor of local (we'd fast-forward — R4 case)  → push, "success"
          diverged                                                   → force-push, "updated"

        Equality is at the *tree* level so commit metadata (timestamp/author)
        differences don't trip the reused path. Divergence detection uses
        `merge-base --is-ancestor` rather than fragile HEAD~ arithmetic, so it
        works whether the branch has 1 or many commits ahead of base.
        """
        if not self._branch_exists_remote(branch):
            self.run("push", "origin", branch)
            return "success"

        try:
            self.run("fetch", "origin", branch)
        except RuntimeError as exc:
            logger.warning("fetch failed for %s: %s — falling back to force-push", branch, exc)
            self.run("push", "--force-with-lease", "origin", branch)
            return "updated"

        # tree equality short-circuits everything else
        try:
            remote_tree = self.run("rev-parse", f"origin/{branch}^{{tree}}")
        except RuntimeError:
            remote_tree = ""
        if remote_tree and remote_tree == local_tree:
            return "reused"

        # is_ancestor returns exit-code 0 when origin/branch is an ancestor of HEAD,
        # 1 otherwise. Repo.run raises on non-zero exit — we use that as the signal.
        try:
            self.run("merge-base", "--is-ancestor", f"origin/{branch}", "HEAD")
            is_fast_forward = True
        except RuntimeError:
            is_fast_forward = False

        if is_fast_forward:
            self.run("push", "origin", branch)
            return "success"

        # Diverged: stale fix on remote that we want to overwrite.
        self.run("push", "--force-with-lease", "origin", branch)
        return "updated"

    def gitlab_create_merge_request(self, source_branch: str, target_branch: str = "main", title: str = None, description: str = None):
        """
        Create-or-reuse a merge request in GitLab. Idempotent.

        Lookup → reuse rules:
          existing open MR     → return it, status="reused"
          existing merged MR   → return it, status="already_merged"
                                 (the fix is already on target_branch; caller
                                  must NOT push more commits to source_branch)
          existing closed MR   → open a new MR, status="opened"
                                 (closed = previous attempt was rejected; we
                                  need a fresh review thread)
          no existing MR       → open new, status="opened"

        Field-level correctness of the existing MR (title/body/labels) is
        explicitly OUT OF SCOPE — fixing stale fields belongs to a separate
        "MR refresh" feature, not idempotency. See README idempotency section.
        """
        if not self.token:
            raise EnvironmentError("❌ Missing GITLAB_TOKEN environment variable")

        # 1) Lookup first — cheaper than POST→409.
        existing = self.find_open_or_merged_mr_for_branch(source_branch)
        if existing is not None:
            if existing["state"] == "opened":
                logger.debug(f"♻️ Reused open MR !{existing['iid']} → {existing['url']}")
                return {**existing, "status": "reused"}
            if existing["state"] == "merged":
                logger.debug(f"⏭️ MR already merged !{existing['iid']} → {existing['url']}")
                return {**existing, "status": "already_merged"}
            # Closed (or any other terminal state): fall through to create new.

        # 2) Create.
        m = re.match(r"(https?://[^/]+)/(.+?)(?:\.git)?$", self.repo_url)
        if not m:
            raise ValueError(f"Invalid repo_url format: {self.repo_url}")
        host, project_path = m.groups()
        project_id = project_path.replace("/", "%2F")
        api_url = f"{host}/api/v4/projects/{project_id}/merge_requests"
        headers = {"PRIVATE-TOKEN": self.token}
        data = {
            "source_branch": source_branch,
            "target_branch": target_branch,
            "title": title or f"Merge {source_branch} into {target_branch}",
            "description": description or "Auto-created by ci_agent",
            "remove_source_branch": True,
        }

        logger.debug(f"📤 Creating Merge Request at {api_url}")
        if cfg.env != "local_ts_host__aca":
            resp = requests.post(api_url, headers=headers, data=data)
        else:
            proxies = {
                "http":  f"socks5://{cfg.socks5_proxy}",
                "https": f"socks5://{cfg.socks5_proxy}",
            }
            resp = requests.post(api_url, headers=headers, data=data, proxies=proxies)

        # GitLab returns 409 when an open MR already exists for the same
        # source/target. Race protection: re-do the lookup and reuse it.
        if resp.status_code == 409:
            logger.info("MR POST got 409 — racing with another worker; re-looking up")
            again = self.find_open_or_merged_mr_for_branch(source_branch)
            if again is not None and again["state"] == "opened":
                return {**again, "status": "reused"}
            raise RuntimeError(f"GitLab 409 but no MR found post-race: {resp.text}")

        if resp.status_code != 201:
            raise RuntimeError(f"GitLab API error {resp.status_code}: {resp.text}")

        mr = resp.json()
        logger.debug(f"✅ Merge Request created: !{mr['iid']} → {mr['web_url']}")
        return {**self._mr_to_dict(mr), "status": "opened"}

    def gitlab_fetch_file(
        self,
        file_path: str,
        branch: str = "main",
    ):
        """
        Fetch the content of a specified file from a GitLab repository.

        Args:
            file_path: path of the file in the repository, e.g. "api/views.py"
            branch: branch name, default main

        Returns:
            file content as a string

        self.token: Personal Access Token or Runner Token
        """
        # extract host and project_path from repo_url
        # host: GitLab instance address, e.g. "http://localhost:8080"
        # project_path: repository ID or namespace path, e.g. "lishu2016/order_be"
        # assuming repo_url does not have ".git" in the end
        m = re.match(r"(https?://[^/]+)/(.+?)(?:\.git)?$", self.repo_url)
        if not m:
            raise ValueError(f"Invalid repo_url format: {self.repo_url}")

        host, project_path = m.groups()
        url = f"{host}/api/v4/projects/{requests.utils.quote(project_path, safe='')}/repository/files/{requests.utils.quote(file_path, safe='')}"
        logger.debug(f"[gitlab_fetch_file], self.repo_url={self.repo_url}, host={host}, project_path={project_path},url={url}")
        params = {"ref": branch}
        headers = {"PRIVATE-TOKEN": self.token} if self.token else {}

        if cfg.env != "local_ts_host__aca" :
            response = requests.get(url, headers=headers, params=params)
        else:
            proxies = {
                "http":  f"socks5://{cfg.socks5_proxy}",
                "https": f"socks5://{cfg.socks5_proxy}",
            }
            response = requests.get(url, headers=headers, params=params, proxies=proxies)

        if response.status_code != 200:
            raise Exception(f"❌ Failed to fetch file: {response.status_code} {response.text}")

        data = response.json()
        content = base64.b64decode(data["content"]).decode("utf-8", errors="ignore")
        return content


# ── GitLabProvider ───────────────────────────────────────────────────────────


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
        """Idempotent. Also probes for an existing open/merged MR on the
        deterministic branch and returns it under `existing_mr` so the graph
        can short-circuit (R10) when the fix is already merged."""
        repo = Repo(repo_path=str(repo_path), repo_url=self._project_web_url)
        result = repo.create_fix_branch(bug_id=bug_id)

        # R10 probe: if a remote branch already exists, look up its MR. Only
        # a remote branch (not a local-only one) can have an MR attached, so
        # this skips the lookup on the cheap "first run" path.
        existing_mr = None
        if repo._branch_exists_remote(result["branch_name"]):
            existing_mr = repo.find_open_or_merged_mr_for_branch(result["branch_name"])
        if existing_mr is not None:
            result["existing_mr"] = existing_mr
        logger.info("create_fix_branch: status=%s branch=%s existing_mr=%s",
                    result["status"], result["branch_name"],
                    existing_mr["state"] if existing_mr else None)
        return result

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

    # ── precheck (R10 early short-circuit) ────────────────────────────────────

    def find_merged_mr_by_bug_prefix(self, bug_id: str) -> dict | None:
        """Cheap pre-clone probe: any merged MR for `auto/bf/{bug_id}-*`?

        No working tree needed — pure REST call. Used by precheck_already_fixed
        before fetch_trace. Returns the MR dict or None.

        We instantiate Repo with a placeholder repo_path because the
        constructor only sets attributes (env-specific URL rewrites and
        token) — no clone happens until ensure_repo_ready().
        """
        repo = Repo(repo_path=str(Path(cfg.repo_base_path) / "_precheck"),
                    repo_url=self._project_web_url)
        return repo.find_merged_mr_by_bug_prefix(bug_id)


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
