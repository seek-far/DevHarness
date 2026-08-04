"""Regression: the fetch_trace node must always ASK THE PROVIDER.

Incident (introduced 1c091d1, 2026-07-26; found 2026-08-04): the node
short-circuited on an empty project_id/job_id and returned `{"trace": ""}`
without ever calling `provider.fetch_trace()`. GitLab needs those two ids, so
the check looked reasonable — but the local providers need neither, and their
`fetch_trace()` is the ONLY place `--trace-file` and `--test-cmd` are consumed.

Result: `bf_worker.standalone` and every `evaluation` sweep (both run
LocalNoGitProvider, neither carries a project_id/job_id) silently lost their
trace source and died in parse_trace with "trace is empty — nothing to
analyse". It survived nine days because the two paths under active use are
exactly the two that skip the branch: GitLab mode always has both ids, and
ver99 routes to mini_react_loop before parse_trace runs.

The fix moves the guard into GitLabProvider, where "I need a job coordinate"
is actually true. These tests pin both halves so it cannot regress:

  * the node calls the provider even with no ids  (the bug)
  * GitLabProvider returns "" without issuing an HTTP request  (the original
    intent — no `.../jobs//trace` URL that 400s)
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bf_worker"))

from graph.nodes.fetch_trace import fetch_trace  # noqa: E402
from providers.local_provider import LocalNoGitProvider  # noqa: E402


def _cfg(provider):
    return {"configurable": {"provider": provider}}


# ── the regression itself ────────────────────────────────────────────────────


class _RecordingProvider:
    def __init__(self, trace="some trace"):
        self.calls = []
        self._trace = trace

    def fetch_trace(self, **kwargs):
        self.calls.append(kwargs)
        return self._trace


def test_node_calls_provider_even_without_project_or_job_id():
    """THE regression. Empty ids are the standalone/eval shape, not an error."""
    provider = _RecordingProvider()

    out = fetch_trace({"project_id": "", "job_id": ""}, config=_cfg(provider))

    assert len(provider.calls) == 1, (
        "fetch_trace must delegate to the provider — short-circuiting here is "
        "what silently disabled --trace-file / --test-cmd for standalone and eval"
    )
    assert out["trace"] == "some trace"


def test_node_calls_provider_when_state_omits_the_keys_entirely():
    """standalone builds its state without project_id/job_id at all."""
    provider = _RecordingProvider()
    out = fetch_trace({}, config=_cfg(provider))
    assert len(provider.calls) == 1
    assert out["trace"] == "some trace"


def test_node_forwards_the_ids_it_does_have():
    provider = _RecordingProvider()
    fetch_trace({"project_id": "p1", "job_id": "j1"}, config=_cfg(provider))
    assert provider.calls == [{"project_id": "p1", "job_id": "j1"}]


# ── end-to-end through the real local provider ───────────────────────────────


def test_trace_file_reaches_the_node(tmp_path: Path):
    """`standalone --trace-file` — a real LocalNoGitProvider, no mocks."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "mod.py").write_text("x = 1\n", encoding="utf-8")
    trace = tmp_path / "err.log"
    trace.write_text("E   AssertionError: expected [] got [1]\n", encoding="utf-8")

    provider = LocalNoGitProvider(
        source_dir=str(src),
        output_dir=str(tmp_path / "out"),
        trace_file=str(trace),
        bug_id="BUG-T1",
    )

    out = fetch_trace({"project_id": "", "job_id": ""}, config=_cfg(provider))

    assert "AssertionError: expected [] got [1]" in out["trace"]


def test_test_cmd_reaches_the_node(tmp_path: Path):
    """`standalone --test-cmd` — the other half of the same seam.

    No requirements.txt in the source dir on purpose: that keeps _ensure_venv
    a no-op so the test stays fast and hermetic.
    """
    src = tmp_path / "src"
    src.mkdir()
    (src / "mod.py").write_text("x = 1\n", encoding="utf-8")

    provider = LocalNoGitProvider(
        source_dir=str(src),
        output_dir=str(tmp_path / "out"),
        test_cmd="echo SENTINEL_TEST_OUTPUT",
        bug_id="BUG-T2",
    )

    out = fetch_trace({"project_id": "", "job_id": ""}, config=_cfg(provider))

    assert "SENTINEL_TEST_OUTPUT" in out["trace"]


def test_a_real_trace_survives_into_parse_trace(tmp_path: Path):
    """The end the bug actually broke: parse_trace raises on an empty trace, so
    a non-empty one here is the whole point."""
    from graph.nodes.parse_trace import parse_trace

    src = tmp_path / "src"
    src.mkdir()
    trace = tmp_path / "err.log"
    trace.write_text(
        'File "mod.py", line 3, in last_n\n'
        "E   AssertionError: expected [] got [1]\n",
        encoding="utf-8",
    )
    provider = LocalNoGitProvider(
        source_dir=str(src),
        output_dir=str(tmp_path / "out"),
        trace_file=str(trace),
        bug_id="BUG-T3",
    )

    state = fetch_trace({"project_id": "", "job_id": ""}, config=_cfg(provider))
    parse_trace({"trace": state["trace"]})  # must not raise


# ── the guard's new home: GitLabProvider ─────────────────────────────────────


def _gitlab_provider():
    from providers.gitlab_provider import GitLabProvider

    return GitLabProvider.__new__(GitLabProvider)  # no __init__ / no network


@pytest.mark.parametrize(
    "project_id,job_id",
    [("", ""), ("p1", ""), ("", "j1")],
)
def test_gitlab_returns_empty_without_issuing_a_request(project_id, job_id):
    """Original intent preserved: never build `.../jobs//trace`, which 400s."""
    with patch("providers.gitlab_provider.requests.get") as get:
        trace = _gitlab_provider().fetch_trace(project_id=project_id, job_id=job_id)

    assert trace == ""
    get.assert_not_called()


def test_gitlab_still_fetches_when_both_ids_are_present():
    """The guard must not swallow the normal webhook path."""
    with patch("providers.gitlab_provider.requests.get") as get:
        get.return_value.text = "real ci trace"
        get.return_value.raise_for_status = lambda: None
        trace = _gitlab_provider().fetch_trace(project_id="p1", job_id="j1")

    get.assert_called_once()
    assert trace == "real ci trace"


def test_ver99_shape_still_yields_an_empty_trace():
    """ver99 carries a project_id but no CI job, and never reads the trace
    (routing sends it to mini_react_loop before parse_trace). Behaviour must be
    byte-identical to before the fix."""
    with patch("providers.gitlab_provider.requests.get") as get:
        out = fetch_trace(
            {"project_id": "p1", "job_id": "", "workflow_ver": 99},
            config=_cfg(_gitlab_provider()),
        )

    assert out["trace"] == ""
    get.assert_not_called()
