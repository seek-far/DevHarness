# Test Suite Classification

This document classifies the 216 test cases under `tests/` into three categories
and explains how to read each one.

## Classification criteria

- **Unit** — exercises a single function or class in isolation. No graph
  wiring, no provider stubs. Inputs are direct values; assertions are about
  the function's return value or side effects on a temp directory.
- **E2E without mocks** — wires several real components together (e.g. the
  ReAct prompt builder + `prompt_guard.sanitize_untrusted`, or the real
  `populate_source_from_git` driving a real `git init` repo in `tmp_path`).
  Inputs may be synthesized but the components themselves are real.
- **E2E rainy-day with mocks** — needs a stub provider to inject the
  exception sequence we want to verify (network blip, GitLab 503, Redis
  disconnect, `OSError EAGAIN`, etc.). These scenarios are difficult to
  reproduce reliably in CI against a live system, so we fake them at the
  provider boundary while letting everything downstream of the boundary be
  real code.

The full pipeline regression — gateway → orchestrator → worker → real LLM →
real GitLab API — lives in the project root as `integration_test.py` and is
not counted in the 213 figures below.

---

## (1) Unit tests — **162 cases**

| File | Cases | What it covers |
|---|---:|---|
| `test_budget.py` | 13 | `RunBudget.check / record_call / to_dict` — pure class methods. |
| `test_evaluation_coordinator.py` | 3 | `normalize_agent_ref`, `group_specs_by_agent_ref`, `_merge_summary` — dict transformations. |
| `test_fetch_guard.py` | 30 | `validate_fetch_path` — pure path-policy rules. |
| `test_fetch_trace_retry.py` | 17 | `parse_retry_after` (4) + `classify_transient` (13) — pure classifier inputs/outputs. |
| `test_journal_dir_naming.py` | 21 | Directory-name slug rules + `JournalWriter.write` against `tmp_path`. |
| `test_local_provider_venv_exclude.py` | 8 | `_ensure_venv_excluded_from_git` — single filesystem-touching helper. |
| `test_parse_trace_fallback.py` | 9 | `parse_trace` node body (5) + routing function (3) + system-prompt string check (1). |
| `test_patch_guard.py` | 23 | `validate_patch_scope` — pure policy rules. |
| `test_prompt_guard.py` | 25 | `sanitize_untrusted`, `detect_injection`, `wrap_untrusted` — pure functions on strings. |
| `test_source_fetch_fallback.py` | 1 | System-prompt string check. |
| `test_transient_retry_helper.py` | 10 | `classify_transient` Redis branch (4) + `with_transient_retry` control flow with `lambda` callables (6). |
| `test_evaluation_sweep_e2e.py` | 2 | `metrics.format_table([])` placeholder; `metrics.aggregate` raises on unknown `run_id`. |

---

## (2) E2E without mocks — **25 cases**

| File | Cases | What it covers |
|---|---:|---|
| `test_promote.py` | 2 | Drives `populate_source_from_git` against a real `git init` repo in `tmp_path` via `subprocess`. No stubs. The only place in the suite that shells out to a real external tool. |
| `test_prompt_guard_wiring.py` | 7 | Feeds malicious traces / source / memory hints into the real `_build_initial_messages` call site and verifies they are wrapped via `sanitize_untrusted`. All components real. |
| `test_react_loop_retry_feedback.py` | 11 | Drives the real `_format_retry_feedback` + `_build_initial_messages` chain with crafted state dicts. No stubs. |
| `test_parse_trace_fallback.py` | 2 | Real `_build_initial_messages` exercised on the parse-fallback vs. normal state shapes. |
| `test_source_fetch_fallback.py` | 3 | Real `_build_initial_messages` exercised on the fetch-failure / parse-failure / normal branches. |

---

## (3) E2E rainy-day with mocks — **29 cases**

| File | Cases | Scenario the mock simulates |
|---|---:|---|
| `test_fetch_trace_retry.py` | 9 | Stub provider raises queued `requests.ConnectionError` / `Timeout` / `HTTPError 5xx` / `HTTPError 4xx` / `OSError(EAGAIN)`. Network blips, rate limits, NFS hiccups — hard to reproduce reliably in CI. |
| `test_node_io_retry.py` | 13 | Retry telemetry across the four wrapped nodes (`fetch_source_file`, `commit_change`, `wait_ci_result`, `create_mr`). Stub providers raise transient/permanent exceptions; reaching these in a live test would require disconnecting GitLab or kicking a Redis connection. |
| `test_parse_trace_fallback.py` | 1 | `apply_change_and_test` reject-path for a fix entry missing `file_path`. `_StubProvider` is used only to short-circuit before venv + pytest setup — we want to test the rejection logic, not the apply machinery. |
| `test_source_fetch_fallback.py` | 5 | `_ProviderRaises` injects `FileNotFoundError` / `UnicodeDecodeError` to simulate a parser-supplied path that is unreachable; the apply-rejection variant uses a stub provider for the same reason as above. |
| `test_evaluation_sweep_e2e.py` | 1 | The "typical usage" arc: real `Fixture.discover` + real `runner.run_sweep` + real `metrics.aggregate` driven by stub `Agent`s in `tmp_path`. Verifies on-disk layout, `RunRecord` field completeness, outcome propagation, and per-agent fix-rate aggregation. The stub `Agent` is the only fake — every other component is real code. |

---

## Tally

| Category | Cases | Share |
|---|---:|---:|
| Unit | 162 | 75% |
| E2E without mocks | 25 | 12% |
| E2E rainy-day with mocks | 29 | 13% |
| **Total** | **216** | 100% |

---

## Borderline calls

These are the cases where the category is debatable; the rationale is
recorded so future contributors don't have to re-litigate.

1. **Two "happy-path-with-stub" cases inside `test_node_io_retry.py`**
   — even with no exception queued, the stub is mandatory because we cannot
   really push to git or wait on Redis from a unit test. Classified as (3)
   on the basis of "must mock to run" — same engineering shape as the
   rainy-day variants in the same file.
2. **Wiring-style cases in `test_react_loop_retry_feedback.py`**
   — `_format_retry_feedback` is a pure function, but the file's docstring
   explicitly states the goal is to verify the wiring between the prompt
   builder and `sanitize_untrusted`. Classified as (2) by intent rather
   than function purity.
3. **`test_journal_dir_naming.py`** — writes to `tmp_path`, which is
   technically a filesystem dependency. Classified as (1) because
   `tmp_path` is part of the test runtime, not an external dependency, and
   each case targets a single class.
4. **`test_promote.py`** — really runs `subprocess` against a real `git`
   binary. Classified as (2) since no mock is involved; it's the most
   "integration-shaped" entry in the unit suite.

---

## What is *not* in this count

- `integration_test.py` (project root) — full pipeline test against a
  real Redis, real LLM, and a fake-GitLab service from `tests/_gitlab_fake/`.
  Run separately when validating GitLab-mode regressions.
- `tests/llm_fixtures/` — real-LLM fixtures driven by
  `run_fixtures.py` against the standalone runner. Excluded from
  pytest collection by `tests/llm_fixtures/conftest.py`. Used to verify
  fallback flag wiring under a real LLM.

---

# Coverage gap: `evaluation/`

The `evaluation/` package is the thinnest-tested area of the codebase.
Documented here so the gap is visible and so anyone adding tests can pick
the highest-value targets first.

## Coverage matrix

| Module | LOC | Direct tests | Coverage |
|---|---:|---:|---|
| `cli.py` | 211 | **0** | The whole `bench` CLI surface (`list-fixtures`, `list-journal`, `promote`, `run`, `report`) has no tests. |
| `coordinator.py` | 303 | 3 (`test_evaluation_coordinator.py`) | Only the three pure helpers `normalize_agent_ref`, `group_specs_by_agent_ref`, `_merge_summary` are covered. The actual sweep orchestration — subprocess spawning, worktree handling, summary merging into `runs/<run_id>/` — is untested. |
| `fixture.py` | 72 | covered indirectly by `test_evaluation_sweep_e2e.py` | `Fixture.discover` is exercised end-to-end, including round-tripping `category` and `expected_outcome` from `meta.json`. Schema-edge cases (missing fields, malformed `meta.json`) are still uncovered. |
| `metrics.py` | 55 | 3 (`test_evaluation_sweep_e2e.py`) | `aggregate` is exercised on real on-disk `summary.json` produced by `run_sweep`; `format_table` empty-input and `aggregate` missing-run cases are covered. |
| `promote.py` | 150 | 2 (`test_promote.py`) | Only the `populate_source_from_git` happy path is covered. `base_commit_from_record`'s field-fallback logic, remote-URL resolution, unreachable-commit handling, and the "fixture already populated" branches are untested. |
| `runner.py` | 155 | 1 (`test_evaluation_sweep_e2e.py`) | `run_sweep` happy path is exercised end-to-end with stub Agents: layout, `RunRecord` field completeness, outcome propagation, and `summary.json` shape are all asserted. The `make_agent` factory and the cell-crashed branch (`agent.fix` raises) are still uncovered. |

`evaluation/` totals roughly 955 LOC. After `test_evaluation_sweep_e2e.py`
landed, 8 cases point directly at it (3 in `test_evaluation_coordinator.py`,
2 in `test_promote.py`, 3 in `test_evaluation_sweep_e2e.py`) and the typical
`bench run` → `bench report` arc is exercised end-to-end. The remaining
gaps are `cli.py` (CLI shell), the subprocess + worktree paths in
`coordinator.py`, `make_agent`'s real branch in `runner.py`, and the
remaining branches of `promote.py`.

## Risk ranking

✅ **`metrics.aggregate`** — covered by `test_evaluation_sweep_e2e.py`.
The e2e drives `aggregate` on real `summary.json` produced by `run_sweep`,
which catches both the aggregation arithmetic and any future
JSON-serialization regression that would silently corrupt the inputs.

✅ **`runner.run_sweep`** — the happy path is covered, and the test
includes a `RunRecord` field-completeness assertion: every key in
`RunRecord.__dataclass_fields__` must appear in each cell's `record.json`.
Future `RunRecord` field additions that aren't threaded through `runner`
will fail this immediately. The `agent.fix()` exception branch and the
`make_agent` real path are still uncovered.

🟡 **`fixture.Fixture` loader schema edges** — the happy path is
exercised indirectly by the e2e (parses `meta.json`, attaches `trace.txt`,
validates `source/`), but malformed inputs (missing `meta.json`, missing
`source/` dir, malformed JSON) are still uncovered. A small dedicated
unit file would round it out cheaply.

🟡 **Remaining `promote` branches** — the current 2 cases hit only the
happy path. `base_commit_from_record` falls back through
`branch_create_result` and `commit_result`; URL parsing has multiple
shapes; the "remote ref not reachable" path returns errors that the CLI
needs to surface. None of these are covered.

🟢 **`cli.py`** — thin command-line shell that delegates to the modules
above. Low independent value; a bash smoke test is usually sufficient.

🟢 **Subprocess + worktree paths in `coordinator.py`** — testable but
expensive (would require mocking `subprocess.run` and `git worktree`).
Modest payoff relative to the effort.

## Suggested follow-ups

The two 🔴 items above are now closed by `test_evaluation_sweep_e2e.py`.
If more evaluation coverage is needed, the next-most-valuable additions
are:

1. **Schema-edge cases for `fixture.Fixture.load`** — 3–4 unit cases
   covering missing `source/`, missing `meta.json`, malformed JSON, and
   defaults round-trip when fields are absent.

2. **`promote.base_commit_from_record` fallback chain** — 3–5 unit cases
   covering the field priority order (`base_commit` → `branch_create_result`
   → `commit_result`) and the "no candidate found" outcome.

3. **`runner` failure branches** — `agent.fix()` raising must produce a
   `RunRecord` with `outcome="error"` and `error=<str>`; the cell directory
   must still be written. One additional case in
   `test_evaluation_sweep_e2e.py` would cover this.
