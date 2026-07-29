"""Parity between the vendored mini-swe-agent copy and the installed upstream.

Two layers, both cheap (no docker, no LLM, no network):

  L1 — source parity. `bf_worker/agents/vendor/mini/{agent,docker_env}.py`
       must be byte-identical to the installed upstream modules once the
       SDLCMA vendor header is stripped. This is the mechanism that keeps
       `vendor/UPSTREAM.md`'s allowed-difference whitelist honest: any edit
       (or a formatter run) fails here until it is recorded there.

       It is *supposed* to fail after an upstream upgrade — that forces a
       human to decide whether to re-sync rather than silently drifting.

  L2 — behavioural parity. The same scripted model + environment run through
       MiniSweAgent twice, once per MINI_IMPL, must produce identical message
       sequences and identical (non-volatile) final state. Parameterised over
       every workflow_mode because the staged modes construct DefaultAgent at
       their own call sites — a construction site that was missed when wiring
       the switch shows up here as that mode still running upstream.

See /mnt/d/PL/sdlcma/W1-vendored-mini-design.md §5.
"""

from __future__ import annotations

import importlib
import inspect
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "bf_worker"))

pytest.importorskip("minisweagent", reason="upstream mini-swe-agent not installed")

from agents.base import BugInput  # noqa: E402
from agents import mini_swe_agent as M  # noqa: E402

from minisweagent.environments.local import LocalEnvironment  # noqa: E402
from minisweagent.models.test_models import DeterministicModel, make_output  # noqa: E402


_VENDOR_DIR = _ROOT / "bf_worker" / "agents" / "vendor" / "mini"
_HEADER_BEGIN = "# === SDLCMA VENDOR HEADER (begin) ===\n"
_HEADER_END = "# === SDLCMA VENDOR HEADER (end) ===\n"

# vendored file → upstream module it was copied from
_VENDORED = [
    ("agent.py", "minisweagent.agents.default"),
    ("docker_env.py", "minisweagent.environments.docker"),
]


# ── L1: source parity ────────────────────────────────────────────────────────

def _strip_vendor_header(text: str) -> str:
    """Remove the vendor header block — the ONLY whitelisted difference.

    When W2 starts modifying the vendored loop, new whitelist entries get
    normalised here, and this function becomes the authoritative statement of
    "what we changed relative to upstream". Keep it in sync with
    vendor/mini/UPSTREAM.md.
    """
    assert text.startswith(_HEADER_BEGIN), "vendored file must start with the vendor header"
    _, _, body = text.partition(_HEADER_END)
    assert body, "vendor header is missing its (end) marker"
    return body


@pytest.mark.parametrize("filename,upstream_module", _VENDORED)
def test_vendored_source_matches_upstream(filename, upstream_module):
    upstream_path = Path(inspect.getsourcefile(importlib.import_module(upstream_module)))
    ours = _strip_vendor_header((_VENDOR_DIR / filename).read_text(encoding="utf-8"))
    theirs = upstream_path.read_text(encoding="utf-8")

    assert ours == theirs, (
        f"vendored {filename} has drifted from {upstream_module}.\n"
        f"Either an edit/formatter touched it (record the change in "
        f"bf_worker/agents/vendor/mini/UPSTREAM.md and add a normalisation "
        f"rule to _strip_vendor_header), or upstream was upgraded (re-sync "
        f"per UPSTREAM.md and update the pin)."
    )


@pytest.mark.parametrize("filename,_upstream_module", _VENDORED)
def test_vendor_header_records_the_pin(filename, _upstream_module):
    """The header must carry provenance — an unpinned copy is unauditable."""
    text = (_VENDOR_DIR / filename).read_text(encoding="utf-8")
    header = text.partition(_HEADER_END)[0]
    for needle in ("upstream :", "version  :", "commit   :", "license  : MIT"):
        assert needle in header, f"{filename} vendor header is missing {needle!r}"


def test_license_and_upstream_notes_exist():
    assert (_VENDOR_DIR / "LICENSE").read_text(encoding="utf-8").strip(), "vendored LICENSE is empty"
    assert "8a40ea28" in (_VENDOR_DIR / "UPSTREAM.md").read_text(encoding="utf-8"), \
        "UPSTREAM.md must pin the commit of the installed package"


def test_vendored_from_the_installed_package():
    """Guard the mistake W1 actually made: copying from some other checkout.

    SDLCMA runs an editable-installed *fork*; other mini-swe-agent checkouts at
    other versions can exist on the same machine. Vendoring from one of those
    yields files that silently do not match what runs.
    """
    installed = Path(inspect.getsourcefile(importlib.import_module("minisweagent.agents.default")))
    header = (_VENDOR_DIR / "agent.py").read_text(encoding="utf-8").partition(_HEADER_END)[0]
    import minisweagent
    assert f"version  : {minisweagent.__version__}" in header, (
        f"vendor header claims a different version than the installed package "
        f"({minisweagent.__version__}, at {installed})"
    )


# ── MINI_IMPL switch ─────────────────────────────────────────────────────────

def test_default_impl_is_upstream(monkeypatch):
    """Unset MINI_IMPL must keep the pre-vendoring code path exactly."""
    monkeypatch.delenv("MINI_IMPL", raising=False)
    assert M._default_agent_class().__module__ == "minisweagent.agents.default"
    assert M._default_environment_class() == "docker"


def test_vendored_impl_selected(monkeypatch):
    monkeypatch.setenv("MINI_IMPL", "vendored")
    assert M._default_agent_class().__module__ == "agents.vendor.mini.agent"
    assert M._default_environment_class() == M._VENDORED_DOCKER_ENV_CLASS


def test_unknown_impl_is_fatal(monkeypatch):
    """No silent fallback: a typo'd flag must not quietly measure upstream."""
    monkeypatch.setenv("MINI_IMPL", "vendoredd")
    with pytest.raises(ValueError, match="MINI_IMPL"):
        M._default_agent_class()


def test_explicit_environment_class_still_wins(monkeypatch):
    """The switch only moves the *default*; a caller-supplied class survives.

    Every mini unit test in this repo relies on passing environment_class
    "local" so CI needs no docker.
    """
    monkeypatch.setenv("MINI_IMPL", "vendored")
    captured = {}

    def _fake_get_environment(config):
        captured.update(config)
        return object()

    monkeypatch.setattr("minisweagent.environments.get_environment", _fake_get_environment)
    M.build_sb_environment(
        {"environment": {"environment_class": "local"}},
        {"instance_id": "astropy__astropy-1"},
    )
    assert captured["environment_class"] == "local"


# ── L2: behavioural parity ───────────────────────────────────────────────────

_INSTANCE = {"instance_id": "sympy__sympy-1", "problem_statement": "x is off by one"}
_BG_INSTANCE = {
    "instance_id": "django__django-15098",
    "problem_statement": "Internationalisation did not support script+region locales.",
}

_AGENT_CFG = {
    "system_template": "You are a helpful assistant.",
    "instance_template": "{{task}}",
    "step_limit": 10,
    # DeterministicModel bills $1/call; the default $3 cap would truncate the
    # longer scenarios and make the comparison about the cap, not the loop.
    "cost_limit": 0,
}

_SUBMIT_PATCH = (
    "printf 'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\\n"
    "diff --git a/x.py b/x.py\\n+    return n - 1\\n'"
)
_SUBMIT_HANDOFF = (
    "printf 'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\\n"
    "ROOT_CAUSE: off by one\\nSUSPECT_FILES: x.py\\nREPRO: python repro.py\\n'"
)
_V3_HANDOFF = (
    "printf 'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\\nROOT_CAUSE: off by one\\n"
    "SUSPECT_FILES: x.py\\nREPRO: python repro.py\\nDETAILED_SUMMARY: lots of detail\\n'"
)


def _outputs_for(mode: int) -> list:
    """Scripted model outputs long enough to drive `mode` to a submission."""
    if mode in (0, 2, 4):
        return [make_output("submit", [{"command": _SUBMIT_PATCH}])]
    if mode == 1:
        return [
            make_output("investigating", [{"command": "echo investigating"}]),
            make_output("hand off", [{"command": _SUBMIT_HANDOFF}]),
            make_output("fixing", [{"command": "echo fixing"}]),
            make_output("submit patch", [{"command": _SUBMIT_PATCH}]),
        ]
    if mode == 3:
        return [
            make_output("investigate", [{"command": "echo inv"}]),
            make_output("hand off", [{"command": _V3_HANDOFF}]),
            make_output("fix", [{"command": "echo solve"}]),
            make_output("submit patch", [{"command": _SUBMIT_PATCH}]),
        ]
    raise AssertionError(f"no scenario for workflow_mode={mode}")


def _instance_for(mode: int) -> dict:
    # Mode 4 injects per-instance background knowledge; use an instance that
    # actually triggers it so the mode-4 code path is really exercised.
    return _BG_INSTANCE if mode == 4 else _INSTANCE


# Volatile keys: wallclock differs run to run by construction.
_VOLATILE = {"total_llm_wallclock_s"}

# Per-message volatile keys. The model stamps every message with the wallclock
# at which it was produced, so two runs can never match on it. Kept as a tight
# explicit set — normalising away anything more would start hiding the very
# divergences this test exists to catch.
_VOLATILE_MSG_KEYS = {"timestamp"}


def _strip_volatile(obj):
    """Recursively drop _VOLATILE_MSG_KEYS so message sequences are comparable."""
    if isinstance(obj, dict):
        return {k: _strip_volatile(v) for k, v in obj.items() if k not in _VOLATILE_MSG_KEYS}
    if isinstance(obj, list):
        return [_strip_volatile(v) for v in obj]
    return obj


def _run_once(impl: str, mode: int, tmp_path: Path, monkeypatch) -> tuple[dict, list]:
    """Run one MiniSweAgent under `impl`; return (final_state, messages-per-agent).

    Capturing messages needs a hook on every DefaultAgent instance, and every
    construction site now routes through `_default_agent_class()` — so wrapping
    that one function reaches all of them. A site that still imports upstream
    directly would simply not appear in the capture, which is exactly the
    failure this test is meant to catch.
    """
    monkeypatch.setenv("MINI_IMPL", impl)
    real_cls = M._default_agent_class()
    built: list = []

    class _Capturing(real_cls):  # type: ignore[misc,valid-type]
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            built.append(self)

    monkeypatch.setattr(M, "_default_agent_class", lambda: _Capturing)

    # LocalEnvironment runs commands with cwd=this path; a missing directory
    # makes every command fail, the sentinel never fires, and the scripted
    # outputs run out — which looks like a parity failure but is a test bug.
    tmp_path.mkdir(parents=True, exist_ok=True)

    instance = _instance_for(mode)
    agent = M.MiniSweAgent(
        model=DeterministicModel(outputs=_outputs_for(mode)),
        env=LocalEnvironment(cwd=str(tmp_path)),
        mini_config={"agent": dict(_AGENT_CFG)},
        workflow_mode=mode,
    )
    out = agent.fix(BugInput(bug_id="SWE-1", provider=None,
                             metadata={"swebench_instance": instance}))

    assert built, f"no DefaultAgent was constructed for workflow_mode={mode}"
    state = {k: v for k, v in (out.final_state or {}).items() if k not in _VOLATILE}
    return state, [_strip_volatile(list(a.messages)) for a in built]


@pytest.mark.parametrize("mode", [0, 1, 2, 3, 4])
def test_upstream_and_vendored_behave_identically(mode, tmp_path, monkeypatch):
    up_state, up_messages = _run_once("upstream", mode, tmp_path / "up", monkeypatch)
    monkeypatch.undo()
    ve_state, ve_messages = _run_once("vendored", mode, tmp_path / "ve", monkeypatch)

    assert len(ve_messages) == len(up_messages), (
        f"workflow_mode={mode} constructed {len(up_messages)} agent(s) upstream but "
        f"{len(ve_messages)} vendored — a construction site is not routed through "
        f"_default_agent_class()."
    )
    for i, (u, v) in enumerate(zip(up_messages, ve_messages)):
        assert v == u, f"workflow_mode={mode}: agent #{i} message sequence diverged"
    assert ve_state == up_state, f"workflow_mode={mode}: final_state diverged"
