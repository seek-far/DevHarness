"""Static guards for the Cloud Run deployment of llm_gateway.

Shell + YAML, so these are lint-style checks — no gcloud, no network. Each one
corresponds to a failure that is expensive or confusing to diagnose against a
real deployment:

  * a missing `--port` looks like a crash loop, not a config error
  * `--allow-unauthenticated` would silently put an unauthenticated LLM gateway
    (which has no auth of its own) on a public URL
  * a cache configured on a filesystem that cannot persist it hits ~never, and
    the first surprising bill sends you looking in the wrong place
  * a teardown that misses a resource leaves a service account holding
    roles/aiplatform.user with nothing to explain it
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from llm_gateway.config import load_config  # noqa: E402

def shell_code(path: Path) -> str:
    """Script text with comment lines stripped.

    Necessary rather than fussy: these scripts document their own flags in
    prose ("deployed WITHOUT --allow-unauthenticated", "add-iam-policy-binding
    then fails"), so a naive substring search matches the explanation instead
    of the command and the guard silently passes on documentation alone.
    """
    return "\n".join(
        line for line in path.read_text().splitlines()
        if not line.strip().startswith("#")
    )


HARNESS = REPO / "infra" / "gcp-cloudrun"
SETUP = HARNESS / "setup.sh"
TEARDOWN = HARNESS / "teardown.sh"
CONFIG = REPO / "configs" / "llm_gateway" / "vertex_cloudrun.yaml"
DOCKERFILE = REPO / "Dockerfile.llm-gateway"


# ── the config the revision runs with ────────────────────────────────────────


def test_config_parses_after_project_substitution(tmp_path: Path):
    """The template must be a valid gateway config once setup.sh renders it —
    otherwise the failure surfaces as a crash-looping revision in the cloud."""
    rendered = CONFIG.read_text().replace("__PROJECT_ID__", "test-project")
    path = tmp_path / "rendered.yaml"
    path.write_text(rendered, encoding="utf-8")

    cfg = load_config(path)
    backend = cfg.backends[0]
    assert backend.auth == "gcp"
    assert backend.api_key == ""          # keyless; never the "EMPTY" sentinel
    assert "test-project" in backend.base_url


def test_config_is_a_template_not_a_hardcoded_project():
    """A real project id committed here would be deployed by anyone who runs
    the harness without noticing."""
    assert "__PROJECT_ID__" in CONFIG.read_text()


def test_cache_is_explicitly_disabled_not_merely_unconfigured():
    """A Cloud Run container filesystem is per-instance scratch that is wiped on
    scale-to-zero, so a sqlite cache there cannot work. Saying `disabled` out
    loud beats leaving a db_path that looks configured and never hits."""
    raw = yaml.safe_load(CONFIG.read_text())
    assert raw["cache"]["mode"] == "disabled"
    assert "db_path" not in raw["cache"]


def test_vertex_keeps_the_chat_param_profile():
    """Gemini accepts temperature; keeping `chat` is what preserves
    temperature=0 as evaluation's determinism anchor."""
    raw = yaml.safe_load(CONFIG.read_text())
    assert raw["backends"][0]["param_profile"] == "chat"


# ── setup.sh ─────────────────────────────────────────────────────────────────


def test_setup_passes_the_container_port():
    """Dockerfile.llm-gateway listens on 9000; Cloud Run probes 8080 unless
    told otherwise. Omitting --port fails the health check and reads like the
    app crashed."""
    text = SETUP.read_text()
    assert "--port" in text
    assert 'CONTAINER_PORT="${CONTAINER_PORT:-9000}"' in text


def test_setup_deploys_without_public_access():
    """This gateway has no authentication of its own — a public *.run.app URL
    is an open door onto the project's Vertex quota."""
    code = shell_code(SETUP)
    assert "--no-allow-unauthenticated" in code
    assert not re.search(r"(?<![\w-])--allow-unauthenticated", code)


def test_setup_uses_a_dedicated_least_privilege_service_account():
    """Not the Compute Engine default SA, which carries project Editor."""
    text = SETUP.read_text()
    assert "iam service-accounts create" in text
    assert "roles/aiplatform.user" in text
    assert "--service-account" in text
    # Anything broader than the data-plane role would defeat the point.
    for over_broad in ("roles/editor", "roles/owner", "roles/aiplatform.admin"):
        assert over_broad not in text


def test_setup_retries_the_iam_binding():
    """IAM is eventually consistent, and this bit us on the harness's first
    real run: gcloud printed "Created service account" and the very next
    command failed with "Service account ... does not exist". Without a retry
    the whole deploy aborts on a propagation window that clears in seconds.
    """
    code = shell_code(SETUP)
    assert "add-iam-policy-binding" in code
    retry_pos = code.find("for attempt in")
    bind_pos = code.find("add-iam-policy-binding")
    assert 0 <= retry_pos < bind_pos, (
        "add-iam-policy-binding must run inside a retry loop — IAM propagation "
        "makes a fresh service account 404 for a few seconds"
    )
    # And the reason must survive in the file, or the next person deletes the
    # loop as redundant.
    assert "eventually consistent" in SETUP.read_text().lower()


def test_setup_renders_config_into_a_gitignored_file():
    text = SETUP.read_text()
    assert "vertex_cloudrun.local.yaml" in text
    ignored = (REPO / ".gitignore").read_text()
    assert "configs/llm_gateway/vertex_cloudrun.local.yaml" in ignored


def test_setup_verifies_anonymous_access_is_refused():
    """The access model is only real if it is checked. A deploy that silently
    became public would otherwise look identical to a successful one."""
    text = SETUP.read_text()
    assert "print-identity-token" in text
    assert "401|403" in text


# ── teardown.sh removes everything setup.sh creates ──────────────────────────


@pytest.mark.parametrize(
    "resource,create_marker,delete_marker",
    [
        ("cloud run service", "run deploy", "run services delete"),
        ("artifact registry", "repositories create", "repositories delete"),
        ("service account", "service-accounts create", "service-accounts delete"),
        ("iam binding", "add-iam-policy-binding", "remove-iam-policy-binding"),
    ],
)
def test_teardown_is_symmetric_with_setup(resource, create_marker, delete_marker):
    assert create_marker in SETUP.read_text(), f"setup no longer creates {resource}"
    assert delete_marker in TEARDOWN.read_text(), f"teardown leaks {resource}"


def test_teardown_removes_the_rendered_config():
    assert "vertex_cloudrun.local.yaml" in TEARDOWN.read_text()


# ── the assumption that makes autoscaling safe ───────────────────────────────


def test_inference_policy_holds_no_mutable_state():
    """Cloud Run autoscales to N instances by default, so anything the policy
    kept in process memory would be split across them.

    llm_gateway/policy.py is stateless by design (selection is a pure function
    of the X-Sdlcma-Attempt header), which is precisely why this deployment
    does NOT need --max-instances=1. If a future circuit breaker or per-bug
    tally lands in the policy, this test should fail and the deployment needs
    revisiting — the state would silently fragment otherwise.
    """
    src = (REPO / "llm_gateway" / "policy.py").read_text()
    assert "stateless" in src.lower()
    # The selector must not accumulate anything across calls.
    select_body = src.split("def select(", 1)[1].split("\ndef ", 1)[0]
    assert not re.search(r"self\.\w+\s*(\+=|-=|=[^=])", select_body), (
        "InferencePolicy.select() mutates instance state — Cloud Run runs "
        "multiple instances, so that state would fragment. Either make it "
        "stateless again or pin --max-instances=1 in setup.sh."
    )


def test_dockerfile_comment_no_longer_claims_the_policy_is_stateful():
    """The comment used to justify --workers 1 with per-bug policy state that
    policy.py explicitly does not have. Left uncorrected it would argue against
    autoscaling for a reason that is not true."""
    text = DOCKERFILE.read_text()
    assert "policy holds per-bug error-count state" not in text
