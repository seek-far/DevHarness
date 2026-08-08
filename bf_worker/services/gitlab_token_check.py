"""
services/gitlab_token_check.py

Startup preflight for the GitLab credential: is this token alive, can it
reach the project this run targets, and does it hold enough role to do the
work?

Why this exists
---------------
``GITLAB_PRIVATE_TOKEN`` accepts three GitLab token kinds interchangeably —
they authenticate through the same ``PRIVATE-TOKEN:`` header and the same
``https://oauth2:<token>@…`` git credential, so no code branches on the kind:

  personal access token  the human owner's access to EVERY project they can
                         reach, and it grows as they join new ones
  project access token   a per-project bot user; cannot see any other project
  group access token     a per-group bot user; every project in that group

Being interchangeable at the HTTP layer is exactly what makes them confusing
at the operational layer. Three misconfigurations are routine, and all three
surface late and misleadingly without this check:

  1. The token expired or was revoked. Access tokens are FORCED to carry an
     expiry, so this is a scheduled outage, not a freak event. Without the
     check it lands as a 401 somewhere inside the graph and reads as "GitLab
     is down".
  2. The token cannot reach the target project — a project token pointed at
     the wrong project, or a group token whose group does not own it. GitLab
     answers 404 (not 403) for resources you may not see, so it reads as
     "the repo is gone".
  3. The bot was granted Reporter instead of Developer. Everything reads
     fine; the run dies at `git push` after the LLM spend is already sunk.

There is also a fourth, produced by the credential-injection path itself: an
env var that exists but is EMPTY wins over the image-baked env file (pydantic
precedence — see settings/worker_settings.py:gitlab_private_token), so a
half-populated k8s Secret yields an empty token rather than falling back.

How the check decides
---------------------
The authorization question — "may this credential act on this project?" — is
answered by a CAPABILITY PROBE, ``GET /projects/{id}``, not by inferring the
token's scope from its shape. That matters: a group access token on a parent
group legitimately covers projects in nested subgroups, and its bot username
only names the top-level group, so any username-pattern inference would
reject a valid setup. The probe treats all three kinds identically and also
yields the role, in one round trip.

The token KIND is still derived (from the bot username on ``GET /user``), but
only to label the log line — the one place today that records which
credential a run used, since RunRecord carries no actor (docs/auth.md §5).
Getting it wrong is cosmetic.

Policy
------
Aborts (``SystemExit``) only on the four unambiguous, operator-actionable
cases: empty token, 401, cannot-reach-project, role below Developer. Anything
it cannot decide — network failure, a GitLab that omits ``permissions``, an
older instance without ``/personal_access_tokens/self`` — logs and continues.
That mirrors services/llm_model_check.py: a probe must not become a new way
for runs to die.

Aborting here is safe with respect to the restart machinery: bf_worker's
``finally`` sets ``worker:completed:{bug_id}`` on any non-signal exit, so
HealthMonitor does not restart a misconfigured worker into a loop (see
docs/architecture.md, worker restart policy). Same path the existing
llm_model_check abort already takes.

Set ``GITLAB_SKIP_TOKEN_CHECK=1`` to bypass the whole preflight.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import requests

logger = logging.getLogger(__name__)

# Connect+read timeout per probe. Short: a healthy GitLab answers these in
# milliseconds, and every second here is prepended to every bug's latency.
_PROBE_TIMEOUT_S = 10.0

# Env var to skip the preflight entirely. Same spirit as
# LLM_ALLOW_MODEL_MISMATCH — for one-offs where you know better.
_SKIP_ENV = "GITLAB_SKIP_TOKEN_CHECK"

# GitLab member access levels. The worker pushes branches and opens MRs, both
# of which need Developer. It never merges (R10 only READS merge state), so
# Maintainer is over-granting.
ACCESS_GUEST = 10
ACCESS_REPORTER = 20
ACCESS_DEVELOPER = 30
_ACCESS_NAMES = {
    0: "No access", 5: "Minimal", ACCESS_GUEST: "Guest",
    ACCESS_REPORTER: "Reporter", ACCESS_DEVELOPER: "Developer",
    40: "Maintainer", 50: "Owner",
}

# Warn this far ahead of expiry. Access tokens must expire, so the useful
# question is "is someone going to be surprised", and two weeks is enough
# notice to schedule a rotation without normalising the warning.
_EXPIRY_WARN_DAYS = 14


@dataclass
class TokenInfo:
    """What the preflight learned. Never carries the token itself."""
    kind: str = "unknown"          # personal | project | group | unknown
    username: str = ""
    access_level: int | None = None
    scopes: list[str] | None = None
    expires_at: str = ""
    expires_in_days: int | None = None

    @property
    def access_level_name(self) -> str:
        if self.access_level is None:
            return "unknown"
        return _ACCESS_NAMES.get(self.access_level, str(self.access_level))


def classify_kind(username: str) -> str:
    """Map a GitLab username to the token kind that minted it.

    Project and group access tokens run as bot users named
    ``project_<id>_bot_<rand>`` / ``group_<id>_bot_<rand>``. Anything else is
    a human account, i.e. a personal access token.

    Advisory only — the authorization decision is the capability probe. The
    numeric id in the bot name is deliberately NOT used to infer which
    project the token may touch: a group token covers nested subgroups whose
    projects the name says nothing about.
    """
    if not username:
        return "unknown"
    if username.startswith("project_") and "_bot" in username:
        return "project"
    if username.startswith("group_") and "_bot" in username:
        return "group"
    return "personal"


def _get(api: str, path: str, token: str) -> requests.Response:
    return requests.get(
        f"{api.rstrip('/')}{path}",
        headers={"PRIVATE-TOKEN": token},
        timeout=_PROBE_TIMEOUT_S,
    )


def _days_until(expires_at: str) -> int | None:
    """`"2027-01-31"` → days from today. None when unparseable/absent."""
    if not expires_at:
        return None
    try:
        exp = datetime.strptime(str(expires_at)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    return (exp - date.today()).days


def _abort(msg: str) -> None:
    raise SystemExit(f"\n\nGitLab credential preflight failed:\n{msg}\n")


def _fix_hint(info: TokenInfo) -> str:
    return (
        "  How to fix: see infra/gitlab-token/README.md — granting the bot\n"
        "  access to a project is an operations action (add it as a member,\n"
        "  role Developer), not a code change.\n"
        f"  This run's credential looks like a {info.kind} access token"
        + (f" (user {info.username!r})." if info.username else ".")
    )


def _probe_identity(api: str, token: str, info: TokenInfo) -> None:
    """`GET /user` → token kind. 401 here is the earliest dead-token signal."""
    try:
        resp = _get(api, "/user", token)
    except requests.RequestException as exc:
        logger.warning(
            "gitlab_token_check: GET %s/user failed (%s: %s) — token kind "
            "unknown, continuing", api, type(exc).__name__, exc,
        )
        return

    if resp.status_code == 401:
        _abort(
            "  The GitLab token was rejected (HTTP 401 on GET /user).\n"
            "  It is expired, revoked, or simply wrong. Access tokens are\n"
            "  forced to carry an expiry date — check it first.\n"
            f"  GitLab API: {api}"
        )
    if resp.status_code != 200:
        logger.warning(
            "gitlab_token_check: GET /user returned HTTP %s — token kind "
            "unknown, continuing", resp.status_code,
        )
        return

    try:
        info.username = str(resp.json().get("username") or "")
    except ValueError:
        return
    info.kind = classify_kind(info.username)


def _probe_project(api: str, token: str, project_id: str, info: TokenInfo) -> None:
    """`GET /projects/{id}` — the authoritative reachability + role check."""
    try:
        resp = _get(api, f"/projects/{project_id}", token)
    except requests.RequestException as exc:
        # Cannot decide. GitLab may simply be slow to come up; the run's own
        # first API call will fail with full context if it is truly down.
        logger.warning(
            "gitlab_token_check: GET %s/projects/%s failed (%s: %s) — "
            "skipping reachability check, run will proceed",
            api, project_id, type(exc).__name__, exc,
        )
        return

    if resp.status_code == 401:
        _abort(
            "  The GitLab token was rejected (HTTP 401).\n"
            "  It is expired, revoked, or wrong.\n"
            f"  GitLab API: {api}"
        )
    if resp.status_code in (403, 404):
        _abort(
            f"  This token cannot reach project {project_id} (HTTP "
            f"{resp.status_code}).\n"
            "  GitLab answers 404 for projects you are not allowed to see, so\n"
            "  this means 'not authorized', not 'does not exist'.\n"
            "\n"
            "  The webhook payload named this project; refusing here is the\n"
            "  credential-layer twin of the OIDC project_id check, and it\n"
            "  holds even when WEBHOOK_AUTH_MODE=none.\n"
            "\n" + _fix_hint(info)
        )
    if resp.status_code != 200:
        logger.warning(
            "gitlab_token_check: GET /projects/%s returned HTTP %s — "
            "skipping role check, run will proceed", project_id, resp.status_code,
        )
        return

    try:
        perms = (resp.json() or {}).get("permissions") or {}
    except ValueError:
        return

    # A group access token reports its level under group_access; a project
    # member under project_access. Either may be null. Take the strongest.
    levels = []
    for key in ("project_access", "group_access"):
        block = perms.get(key)
        if isinstance(block, dict) and isinstance(block.get("access_level"), int):
            levels.append(block["access_level"])
    if not levels:
        # Don't invent a failure out of a field we did not get.
        logger.warning(
            "gitlab_token_check: project %s carries no readable `permissions` "
            "— skipping role check", project_id,
        )
        return

    info.access_level = max(levels)
    if info.access_level < ACCESS_DEVELOPER:
        _abort(
            f"  The token's role on project {project_id} is "
            f"{info.access_level_name} ({info.access_level}), but the worker "
            f"needs at least Developer ({ACCESS_DEVELOPER}).\n"
            "  It pushes an auto/* branch and opens a merge request. Without\n"
            "  Developer the run dies at `git push`, after the LLM spend.\n"
            "  (Maintainer is NOT required — the worker never merges.)\n"
            "\n" + _fix_hint(info)
        )


def _probe_token_metadata(api: str, token: str, info: TokenInfo) -> None:
    """`GET /personal_access_tokens/self` → scopes + expiry. Best effort.

    Older GitLab instances lack the endpoint; that is not a problem worth
    failing a run over, so any non-200 is skipped silently.
    """
    try:
        resp = _get(api, "/personal_access_tokens/self", token)
        if resp.status_code != 200:
            logger.debug(
                "gitlab_token_check: /personal_access_tokens/self → HTTP %s "
                "(older GitLab?) — skipping scope/expiry report",
                resp.status_code,
            )
            return
        data = resp.json() or {}
    except (requests.RequestException, ValueError) as exc:
        logger.debug("gitlab_token_check: token metadata probe skipped (%s)", exc)
        return

    scopes = data.get("scopes")
    if isinstance(scopes, list):
        info.scopes = [str(s) for s in scopes]
    info.expires_at = str(data.get("expires_at") or "")
    info.expires_in_days = _days_until(info.expires_at)

    if info.expires_in_days is not None and info.expires_in_days <= _EXPIRY_WARN_DAYS:
        logger.warning(
            "gitlab_token_check: GitLab token expires in %d day(s) (%s). "
            "Rotate it — every harness pointed at this GitLab fails with 401 "
            "the day it lapses. See infra/gitlab-token/README.md.",
            info.expires_in_days, info.expires_at,
        )


def check_or_abort(cfg: Any, project_id: str, bug_id: str = "") -> TokenInfo | None:
    """Run the preflight. Returns what it learned, or None when skipped.

    Raises SystemExit on: empty token, HTTP 401, project unreachable, or a
    role below Developer.
    """
    if os.environ.get(_SKIP_ENV) == "1":
        logger.warning("gitlab_token_check: skipped via %s=1", _SKIP_ENV)
        return None

    api = (getattr(cfg, "gitlab_api", "") or "").strip()
    token = (getattr(cfg, "gitlab_private_token", "") or "").strip()

    if not token:
        _abort(
            "  GITLAB_PRIVATE_TOKEN is empty.\n"
            f"  Checked: environment, then settings/worker_{getattr(cfg, 'env', '?')}.env\n"
            "\n"
            "  Note an environment variable that exists but is EMPTY still\n"
            "  wins over the env file — a half-populated k8s Secret or a\n"
            "  stray `export GITLAB_PRIVATE_TOKEN=` produces exactly this.\n"
            "  See infra/gitlab-token/README.md."
        )
    if not api:
        logger.warning("gitlab_token_check: GITLAB_API is empty — skipping preflight")
        return None
    if not project_id:
        logger.warning("gitlab_token_check: no project_id — skipping preflight")
        return None

    info = TokenInfo()
    # Identity first: it makes the abort messages below able to say WHICH
    # kind of credential failed, which is most of the diagnosis.
    _probe_identity(api, token, info)
    _probe_project(api, token, project_id, info)
    _probe_token_metadata(api, token, info)

    # The only record of which credential a run used — RunRecord has no actor
    # field yet (docs/auth.md §5). Deliberately never logs the token itself.
    logger.info(
        "phase_marker phase=gitlab_token_check bug_id=%s kind=%s user=%s "
        "project=%s role=%s scopes=%s expires_in_days=%s",
        bug_id, info.kind, info.username or "-", project_id,
        info.access_level_name,
        ",".join(info.scopes) if info.scopes else "-",
        info.expires_in_days if info.expires_in_days is not None else "-",
    )
    return info
