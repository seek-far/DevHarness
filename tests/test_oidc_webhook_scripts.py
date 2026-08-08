"""Lint-style guards on infra/oidc-webhook/.

These don't execute the scripts — that needs a live GitLab, a token, and a
monitored project. They assert the load-bearing invariants stay in place, so
an unrelated cleanup doesn't silently regress the live-fire path. Same shape
and reasoning as tests/test_k8s_setup_sh.py.

Each guard below has a concrete failure it prevents; the docstrings say which,
because "why is this asserted" is the part that rots first.
"""

from __future__ import annotations

from pathlib import Path

import pytest

INFRA = Path(__file__).resolve().parents[1] / "infra" / "oidc-webhook"
SETUP_SH = INFRA / "setup.sh"
TEARDOWN_SH = INFRA / "teardown.sh"
INSTALL_SH = INFRA / "install_snippet.sh"
SNIPPET = INFRA / "gitlab-ci-snippet.yml"


def _read(p: Path) -> str:
    return p.read_text()


def _code(p: Path) -> str:
    """Script text with whole-line comments stripped.

    Needed because these files deliberately DOCUMENT the constructs they must
    not contain ("Never `set -x` past this point"), and a naive substring
    search on the raw text flags the warning as if it were the violation.
    """
    return "\n".join(
        line for line in p.read_text().splitlines()
        if not line.lstrip().startswith("#")
    )


# ---------------------------------------------------------------------------
# install_snippet.sh — it WRITES to someone else's repository
# ---------------------------------------------------------------------------

def test_install_passes_current_content_by_env_not_stdin():
    """The existing .gitlab-ci.yml must reach python via the ENVIRONMENT.

    Real incident (2026-08-08): the heredoc `python3 - <<'PYEOF'` already uses
    stdin to feed the script, so an additional `<<< "$CURRENT"` silently won
    and the script read an EMPTY stdin. The "append a marker block" logic then
    returned only the block — i.e. it would have DELETED the monitored
    project's entire pipeline definition. Caught solely because the dry-run is
    the default.

    A comment saying "do not simplify this back to a pipe" does not survive a
    refactor. This test does.
    """
    content = _read(INSTALL_SH)
    assert 'CURRENT="${CURRENT}"' in content, (
        "CURRENT must be exported into python3's environment"
    )
    assert 'current = os.environ["CURRENT"]' in content, (
        "the python side must read CURRENT from the environment"
    )
    assert "sys.stdin.read()" not in content, (
        "reading stdin here collides with the heredoc that carries the script"
    )
    assert '<<< "${CURRENT}"' not in content, (
        "a second stdin redirect silently shadows the heredoc — this is the "
        "exact construct that emptied .gitlab-ci.yml"
    )


def test_install_is_dry_run_by_default():
    """APPLY must be opt-in.

    This script commits to a repository the operator may not own. The default
    has to be 'show me', not 'do it' — and it is the only reason the stdin bug
    above cost nothing.
    """
    content = _read(INSTALL_SH)
    assert 'APPLY="${APPLY:-0}"' in content, "APPLY must default to 0"
    assert '[ "${APPLY}" != "1" ]' in content, (
        "there must be an explicit non-apply branch that exits before committing"
    )
    # The dry-run exit must come BEFORE any POST to the commits API.
    dry_run_at = content.index('[ "${APPLY}" != "1" ]')
    commit_at = content.index("repository/commits")
    assert dry_run_at < commit_at, (
        "the dry-run guard must short-circuit before the commit call"
    )


def test_install_block_markers_are_paired_and_reversible():
    """Idempotency and uninstall both hinge on a matched marker pair.

    Without both markers the regex that strips a previous block cannot match,
    so re-running appends a SECOND copy of the notifier jobs and GitLab fails
    the pipeline on duplicate keys.
    """
    content = _read(INSTALL_SH)
    assert 'BEGIN_MARK="# >>> sdlcma-oidc-notify' in content
    assert 'END_MARK="# <<< sdlcma-oidc-notify' in content
    assert 'UNINSTALL="${UNINSTALL:-0}"' in content, "removal path must exist"


# ---------------------------------------------------------------------------
# gitlab-ci-snippet.yml — the trigger side
# ---------------------------------------------------------------------------

def test_snippet_has_both_notifier_jobs():
    """BOTH notifiers are required — the success one is not optional.

    The worker's wait_ci_result blocks on a Redis stream fed only by POSTs to
    /webhook; it never polls GitLab. Once the trigger moves into CI, "which
    events exist" is decided by `when:`. Ship only the failure half and every
    SUCCESSFUL fix waits out BF_CI_WAIT_TIMEOUT and routes to handle_failure
    instead of opening an MR — a correct patch recorded as a failure.

    Measured 2026-08-08 on a real instance: 17.8s to receive `success` with
    both jobs present, versus a 300s timeout without.
    """
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load(SNIPPET.read_text())

    assert "sdlcma_notify_failed" in doc
    assert "sdlcma_notify_success" in doc
    assert doc["sdlcma_notify_failed"]["when"] == "on_failure"
    assert doc["sdlcma_notify_success"]["when"] == "on_success"


def test_snippet_jobs_each_request_an_id_token():
    """`id_tokens:` is a JOB-level keyword — it cannot be inherited from the
    shared `extends:` base. A job missing it has no OIDC credential and its
    POST is refused 401, which shows up as "the fix never completes" rather
    than as an auth error anyone is looking at.
    """
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load(SNIPPET.read_text())

    for job in ("sdlcma_notify_failed", "sdlcma_notify_success"):
        tokens = doc[job].get("id_tokens")
        assert tokens, f"{job} must request an id_token"
        assert "SDLCMA_TOKEN" in tokens
        assert "$SDLCMA_AUDIENCE" in str(tokens["SDLCMA_TOKEN"].get("aud")), (
            f"{job}'s audience must come from the variable setup.sh writes"
        )


def test_snippet_never_fails_the_pipeline_it_reports_on():
    """A notification failure must not turn a green pipeline red — the
    notifier is observability, not part of the build contract."""
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load(SNIPPET.read_text())
    assert doc[".sdlcma_notify_base"]["allow_failure"] is True


def test_snippet_builds_payload_with_jq_not_string_interpolation():
    """Branch names are attacker-influencable strings that land inside JSON.

    A heredoc/echo-built payload lets a ref containing a quote break out of
    the JSON and forge sibling fields — including `project.id`, which is
    exactly what the authorization check compares against.
    """
    content = _read(SNIPPET)
    assert "jq -n" in content, "the payload must be constructed by jq"
    assert content.count("jq -n") >= 2, "both notifiers must build via jq"


# ---------------------------------------------------------------------------
# setup.sh — the gateway side
# ---------------------------------------------------------------------------

def test_setup_discovers_issuer_instead_of_hardcoding():
    """OIDC_ISSUER must equal the token's `iss` byte for byte, and `iss` is
    the instance's external_url — frequently NOT the URL you reach it on.
    Hand-copying the wrong one yields a 401 reading "token issuer does not
    match", which looks like a bad token rather than a bad setting."""
    content = _read(SETUP_SH)
    assert "/.well-known/openid-configuration" in content
    assert 'd.get("issuer"' in content, "the issuer must come from discovery"


def test_setup_refuses_to_default_the_audience():
    """GitLab mints a token for whatever `aud` a job requests, so a guessable
    audience is forgeable by construction. No default is allowed to exist —
    here or in GatewaySettings."""
    content = _read(SETUP_SH)
    assert 'OIDC_AUDIENCE="${OIDC_AUDIENCE:-}"' in content, (
        "OIDC_AUDIENCE must default to empty, never to a value"
    )
    assert 'if [ -z "${OIDC_AUDIENCE}" ]; then' in content
    assert "die" in content.split('if [ -z "${OIDC_AUDIENCE}" ]; then')[1][:400], (
        "an empty audience must be fatal, not a warning"
    )


def test_setup_requires_rs256_and_does_not_offer_to_relax_it():
    """webhook_auth.py pins algorithms=["RS256"]. Widening that list is how
    `alg: none` and the RS256->HS256 downgrade land, so when an instance does
    not advertise RS256 the script must fail rather than suggest loosening
    the pin."""
    content = _read(SETUP_SH)
    assert "RS256" in content
    assert "Do not 'fix' this by relaxing the pin" in content


def test_setup_does_not_reuse_the_main_token_as_the_ci_read_token():
    """SDLCMA_CI_READ_TOKEN is deliberately left for a human to create as a
    narrow read_api project token. Writing GITLAB_PRIVATE_TOKEN into it would
    expose a full-scope credential to every job in the monitored project."""
    content = _read(SETUP_SH)
    assert "set_var SDLCMA_CI_READ_TOKEN" not in content, (
        "the script must never write the CI read token itself"
    )
    assert "SDLCMA_CI_READ_TOKEN NOT set" in content, (
        "it must say so explicitly rather than leaving it unexplained"
    )


def test_setup_never_echoes_the_token():
    """The token is read from a gitignored env file and used as a header. It
    must not reach stdout — logs of this script get pasted into issues."""
    content = _code(SETUP_SH)
    for forbidden in (
        'echo "${GITLAB_TOKEN}"',
        "echo ${GITLAB_TOKEN}",
        'info "${GITLAB_TOKEN}"',
        "set -x",
    ):
        assert forbidden not in content, f"{forbidden!r} would leak the token"


# ---------------------------------------------------------------------------
# teardown.sh
# ---------------------------------------------------------------------------

def test_teardown_restores_none_and_leaves_the_project_alone():
    """Rollback must only flip the gateway switch.

    Removing the CI variables or the notifier jobs would stop webhooks
    reaching the gateway AT ALL — a far bigger outage than turning auth off,
    and the notifiers are a perfectly good trigger without OIDC.
    """
    content = _read(TEARDOWN_SH)
    assert "WEBHOOK_AUTH_MODE=none" in content
    assert "/variables" not in content, "teardown must not touch project variables"
    assert "repository/commits" not in content, "teardown must not rewrite the repo"


# ---------------------------------------------------------------------------
# shared hygiene
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("script", [SETUP_SH, TEARDOWN_SH, INSTALL_SH])
def test_scripts_are_strict_bash(script: Path):
    """`set -euo pipefail` — a half-applied configuration is worse than none,
    because it looks configured."""
    content = _read(script)
    assert content.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in content
