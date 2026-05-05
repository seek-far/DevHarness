"""Tests for the source_fetch_failed fallback path.

Behavior under test:
  1. fetch_source_file catches provider.fetch_file exceptions and returns
     source_fetch_failed=True with empty content (rather than letting the
     exception propagate to the agent boundary).
  2. fetch_source_file marks success runs explicitly with
     source_fetch_failed=False so downstream branches are unambiguous.
  3. _build_initial_messages emits the fetch-failure prompt section, including
     the parser's suggested path as a hint, when source_fetch_failed=True and
     suspect_file_path is set.
  4. The fetch-failure prompt does NOT echo a source code block.
  5. parse_trace_fallback takes precedence over source_fetch_failed in the
     prompt builder (the parser-failure wording wins) — relevant if both
     flags are ever set; should not happen in normal flow but the routing
     logic should be deterministic.
  6. apply_change_and_test rejects fix entries without file_path when
     source_fetch_failed=True, even if suspect_file_path is non-empty.
  7. The system prompt acknowledges both fallback modes and requires
     file_path on every fix in either mode.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bf_worker"))

from graph.nodes.fetch_source_file import fetch_source_file  # noqa: E402
from graph.nodes.react_loop import _SYSTEM, _build_initial_messages  # noqa: E402


# ── fetch_source_file node ──────────────────────────────────────────────────


class _ProviderRaises:
    """Provider stub whose fetch_file always raises — simulates a path that
    doesn't exist, an unreadable synthetic frame, etc."""

    def __init__(self, exc: Exception):
        self._exc = exc

    def fetch_file(self, path):
        raise self._exc


class _ProviderOk:
    def __init__(self, content: str):
        self._content = content

    def fetch_file(self, path):
        return self._content


def test_fetch_source_file_catches_filenotfound():
    state = {
        "provider": _ProviderRaises(FileNotFoundError("no such file")),
        "suspect_file_path": "made/up/path.py",
    }
    out = fetch_source_file(state)
    assert out["source_fetch_failed"] is True
    assert out["source_file_content"] == ""
    # Must not raise — that was the whole point of this change.


def test_fetch_source_file_catches_unicode_decode_error():
    # Some providers raise UnicodeDecodeError on binary or wrong-encoding files.
    exc = UnicodeDecodeError("utf-8", b"\xff\xff", 0, 1, "invalid start byte")
    state = {
        "provider": _ProviderRaises(exc),
        "suspect_file_path": "weird/file.py",
    }
    out = fetch_source_file(state)
    assert out["source_fetch_failed"] is True


def test_fetch_source_file_success_marks_flag_false():
    state = {
        "provider": _ProviderOk("def f(): return 1\n"),
        "suspect_file_path": "src/m.py",
    }
    out = fetch_source_file(state)
    assert out["source_fetch_failed"] is False
    assert out["source_file_content"] == "def f(): return 1\n"


# ── react_loop prompt branching ─────────────────────────────────────────────


def test_build_messages_fetch_failure_branch_mentions_path_as_hint():
    state = {
        "error_info": "AssertionError",
        "suspect_file_path": "models/user.py",
        "source_fetch_failed": True,
        "source_file_content": "",
        "parse_trace_fallback": False,
        "fix_retry_count": 0,
    }
    msgs = _build_initial_messages(state)
    [human] = [m for m in msgs if m.__class__.__name__ == "HumanMessage"]
    text = human.content
    assert "could NOT be read" in text
    # The parser's path is a hint to the LLM, not ground truth.
    assert "models/user.py" in text
    # Must not echo a source block — there's no content.
    assert "```python" not in text
    # Must require explicit file_path on fixes.
    assert "EXPLICITLY" in text or "explicitly" in text


def test_build_messages_parse_failure_takes_precedence():
    # If both flags are set (shouldn't happen in normal flow, but the routing
    # logic must be deterministic), the parse-failure wording wins because
    # there is no parser hint to forward.
    state = {
        "error_info": "garbage",
        "suspect_file_path": "",  # parse_trace cleared this
        "parse_trace_fallback": True,
        "source_fetch_failed": True,
        "fix_retry_count": 0,
    }
    msgs = _build_initial_messages(state)
    [human] = [m for m in msgs if m.__class__.__name__ == "HumanMessage"]
    text = human.content
    assert "No suspect file pre-identified" in text
    assert "could NOT be read" not in text


def test_build_messages_normal_path_unchanged():
    # Regression guard: when both flags are False and content is present,
    # the prompt shows the source block as before.
    state = {
        "error_info": "AssertionError",
        "suspect_file_path": "calc.py",
        "source_file_content": "def add(a, b): return a - b\n",
        "parse_trace_fallback": False,
        "source_fetch_failed": False,
        "fix_retry_count": 0,
    }
    msgs = _build_initial_messages(state)
    [human] = [m for m in msgs if m.__class__.__name__ == "HumanMessage"]
    text = human.content
    assert "## Suspect file: calc.py" in text
    assert "```python" in text
    assert "could NOT be read" not in text
    assert "No suspect file pre-identified" not in text


def test_system_prompt_covers_both_fallback_modes():
    # The system prompt must tell the LLM that file_path is REQUIRED in both
    # fallback modes, so the model handles them consistently.
    assert "No suspect file pre-identified" in _SYSTEM
    assert "could not be read" in _SYSTEM or "could NOT be read" in _SYSTEM
    assert "EXPLICITLY" in _SYSTEM or "explicitly" in _SYSTEM


# ── apply_change_and_test rejection ─────────────────────────────────────────


def test_apply_rejects_missing_file_path_when_source_fetch_failed():
    from graph.nodes.apply_change_and_test import apply_change_and_test

    class _StubProvider:
        def ensure_repo_ready(self, bug_id):
            return Path("/tmp/does_not_matter_rejected_first")

    state = {
        "provider": _StubProvider(),
        "bug_id": "BUG-1",
        # Parser produced this path but fetch failed — falling back to suspect
        # would write to a path we know is unreachable.
        "suspect_file_path": "models/user.py",
        "source_fetch_failed": True,
        "llm_result": {
            "fixes": [
                {"original": "a", "replacement": "b"},  # no file_path
            ],
        },
        "fix_retry_count": 0,
    }
    out = apply_change_and_test(state)
    assert out["test_passed"] is False
    # Error should reference the unreachable suspect (telemetry signal),
    # not just say "missing file_path".
    assert "could not be read" in out["apply_error"]
    assert "models/user.py" in out["apply_error"]
    assert out["fix_retry_count"] == 1


def test_apply_accepts_explicit_file_path_even_when_source_fetch_failed(tmp_path, monkeypatch):
    # Counter-test: if the LLM does the right thing in fallback mode (sets
    # file_path explicitly on every fix), apply must not reject it. We don't
    # actually run the patch / venv / pytest here — we just assert the
    # rejection short-circuit is NOT taken. We do that by stubbing apply and
    # subprocess so the test doesn't touch the network or filesystem heavily.
    from graph.nodes import apply_change_and_test as mod

    class _StubProvider:
        def ensure_repo_ready(self, bug_id):
            return tmp_path

    # We expect grouping to succeed and the code to proceed past the rejection.
    # Stub apply_change_infos and validate_patch_scope to no-ops; stub
    # subprocess.run so venv creation appears to succeed; stub pytest result
    # to a passing run by making subprocess.run return a stub.
    calls = {"applied": []}

    def _fake_apply(*, src_filepath, change_infos):
        calls["applied"].append((src_filepath, change_infos))

    def _fake_validate(repo_path, fixes_by_file):
        return None

    class _StubProc:
        def __init__(self, rc=0, out="ok", err=""):
            self.returncode = rc
            self.stdout = out
            self.stderr = err

    def _fake_run(cmd, *args, **kwargs):
        # All subprocess calls (venv, pip, pytest) succeed.
        return _StubProc(rc=0, out="1 passed", err="")

    monkeypatch.setattr(mod, "apply_change_infos", _fake_apply)
    monkeypatch.setattr(mod, "validate_patch_scope", _fake_validate)
    monkeypatch.setattr(mod.subprocess, "run", _fake_run)

    # Ensure the explicit target file exists so apply has something to write
    # to (even though our stub doesn't actually write).
    target = tmp_path / "real_target.py"
    target.write_text("# placeholder\n")

    state = {
        "provider": _StubProvider(),
        "bug_id": "BUG-OK",
        "suspect_file_path": "models/user.py",
        "source_fetch_failed": True,
        "llm_result": {
            "fixes": [
                {
                    "file_path": "real_target.py",  # explicit, fallback-safe
                    "original": "# placeholder",
                    "replacement": "# fixed",
                },
            ],
        },
        "fix_retry_count": 0,
    }
    out = mod.apply_change_and_test(state)
    # Must not have been rejected.
    assert out.get("apply_error") is None
    assert out.get("test_passed") is True
    # Apply should have been called with the explicit path.
    [(filepath, _)] = calls["applied"]
    assert filepath.endswith("real_target.py")
