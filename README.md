# DevHarness

**DevHarness** is an automated bug-fixing agent powered by an LLM ReAct loop. It diagnoses test/CI failures, generates patches, validates them locally, and delivers the fix — either as a GitLab merge request or a local patch file.

A built-in evaluation harness benchmarks bug-fix agents against a curated fixture set, and the engine itself is pluggable via an `Agent` interface — so alternative agents (Aider, SWE-agent, custom) can be swapped in and compared head-to-head.

---

## Features

- **Bug-fix engine** — LLM ReAct loop (≤8 steps, 4 bound tools); apply-then-test
  retry feedback loop (failed patch + error tail go into the next prompt);
  parse and source-fetch fallbacks (regex miss or unreadable file degrades to a
  raw-trace prompt instead of failing); provider abstraction so the same graph
  runs against GitLab, local git, or a plain directory.

- **Deployment**
  - Local: standalone CLI; multi-process or docker-compose against a local
    GitLab; docker-compose against gitlab.com.
  - Public-host / AWS ECS / Kubernetes (kind + Helm), all against gitlab.com.
  - Supports both API-based LLMs (OpenAI-compatible, Alibaba Dashscope) and
    self-hosted backends (vLLM, llama.cpp server, Ollama).
  - Optional **LLM Gateway** — independent FastAPI service that proxies
    worker LLM calls across multiple backends per a configured inference
    policy (e.g. cloud → self-hosted ladder). Orthogonal opt-in, off by
    default. Architecture: [LLM Gateway section below](#llm-gateway-orthogonal-opt-in).

- **Extensibility** — `Agent` ABC for plugging in third-party agents; in-graph
  hook system; built-in enhancements (memory, reflection); `agent_ref` pins a
  spec to an SDLCMA git ref for cross-version comparison.

- **Observability** — versioned `RunRecord` telemetry (timings, retry counts,
  branch / commit / MR fields, `max_input_tokens`, agent-code git status,
  reflection and code-review counters); always-on journal writer with
  `FLAGGED` markers for heuristically interesting runs; Redis heartbeat keys
  for live worker monitoring; MCP server exposing the evaluation state
  (`list_fixtures`, `read_journal_entry`, ...) to Claude Desktop or any MCP
  client.

- **Evaluation & Quality** — `bench` CLI for agent × fixture sweeps; curated
  fixtures (`F01`–`F10`) plus journal-promoted real bugs; `RunRecord`
  aggregation into per-agent fix-rate / iterations / wallclock; regression
  coverage across every surface (unit `pytest`, end-to-end
  `integration_test.py`, eval sweeps, per-deployment real-host smokes —
  consolidated in `tests/TESTING.md`).

- **Reliability** — narrow transient-I/O retry shared across 5 nodes; LLM
  transient retry plus a tool-call recovery fallback (vLLM + Qwen wrapper
  mismatch); per-run budget caps (calls / tokens / wallclock); LangGraph
  checkpoint resume at node boundaries; idempotency contract (deterministic
  fix-branch name, three-state push, MR lookup-then-create); already-merged
  short-circuit that skips the whole pipeline when the deterministic branch
  already has a merged MR.

- **Security** — `patch_guard` (write scope + denylist + size caps);
  `prompt_guard` (untrusted-input wrapping + injection logging); `fetch_guard`
  (symmetric read-path denylist); `sanitize_untrusted` on every retry-feedback
  turn.

- **Research** — Code Inspection: a standalone code-review agent independent
  of the bug-fix loop (Phase 1 CLI + operator scorer; opt-in Phase-2
  `code_review` graph node with shadow / acting modes), targeting defects the
  test oracle structurally misses.

---

## Two Modes of Operation

### GitLab Mode (Full Pipeline)

Listens for GitLab CI failure webhooks, diagnoses and fixes the bug automatically, then opens a merge request — no human intervention needed. A failing pipeline on any branch *other than* one starting with `auto/` (the bot's own namespace) is treated as a bug; the fix branch is created off the failing pipeline's ref and the MR is opened back into that same ref, so a `feature/login` failure produces an MR targeting `feature/login`, not main.

```
GitLab CI fails
      │
      ▼
[Gateway]  ── webhook ──►  Redis stream
      │
      ▼
[Orchestrator]  ── spawns ──►  [Worker] (one per bug)
                                    │
                          ┌─────────▼──────────┐
                          │   LangGraph nodes   │
                          │  (via GitLabProvider)│
                          └────────────────────┘
                                    │
                              Merge Request
```

### Standalone Mode (Local)

Runs against a local directory — no GitLab, no CI, no Redis required. Just point it at a project with failing tests.

```
Local project + error trace
      │
      ▼
[Standalone Runner]
      │
      ▼
[Worker] ── same LangGraph nodes ──►  Fix commit (git) or patch file (no-git)
             (via LocalProvider)
```

**Two sub-modes:**
- **With git** (`LocalGitProvider`): Source dir is a git repo. Creates a fix branch, commits locally (no push).
- **Without git** (`LocalNoGitProvider`): Plain directory. Creates a temp copy, generates a unified diff patch file + review report. Original source is never modified.

### LLM Gateway (orthogonal opt-in)

The worker's LLM endpoint can be either the upstream backend directly, or an **`llm_gateway`** service that proxies through a configured inference policy. The gateway is orthogonal to the two modes above — turn it on or off for either; the worker code path is byte-identical with it off.

```
Without gateway (default — single backend per deployment):

    [Worker]  ── OpenAI call ──►  LLM backend (cloud or self-hosted)


With gateway (LLM_VIA_GATEWAY=true):

    [Worker]  ── OpenAI call + hint headers ──►  [LLM Gateway]
       │       (X-Sdlcma-Bug-Id, X-Sdlcma-Attempt)        │
       │                                                  │  inference policy
       │                                                  │  (ordered ladder)
       │                                                  ▼
       │                                      ┌────────────────────────┐
       │      X-Sdlcma-Backend-Name           │  attempt=0 → backend A │
       └────── (response header, lands on ─── │  attempt=1 → backend B │
               RunRecord.llm_backend_name)    │  attempt≥2 → clamp     │
                                              └────────────────────────┘
```

The gateway is a single FastAPI process (`llm_gateway/app.py`, port 9000) that runs alongside the rest of the stack. Worker → gateway is plain OpenAI HTTP, so no client-side dependency on `llm_gateway/` is introduced; off-mode = don't run the gateway and don't set the env flag. See [Configuration → LLM Gateway](#llm-gateway-optional-opt-in) for the bundled policies and how to switch it on, and `docs/architecture.md` "LLM Gateway" for the deep contract.

---

## Quick Start

### Standalone Mode (Simplest)

```bash
# Fix a project using a captured error trace:
python -m bf_worker.standalone \
  --source-dir /path/to/project \
  --trace-file /path/to/error.log

# Let the tool discover errors by running tests:
python -m bf_worker.standalone \
  --source-dir /path/to/project \
  --test-cmd "pytest tests/"

# No-git mode with interactive review:
python -m bf_worker.standalone \
  --source-dir /path/to/project \
  --trace-file error.log \
  --no-git --review --output-dir ./results

# With an agent config (e.g. enable the memory enhancement):
python -m bf_worker.standalone \
  --source-dir /path/to/project \
  --test-cmd "pytest" \
  --no-git --config configs/memory.json
```

**Standalone CLI options:**

| Flag | Default | Description |
|---|---|---|
| `--source-dir` | (required) | Path to the project source directory |
| `--trace-file` | | File containing test/CI error output |
| `--test-cmd` | `pytest` | Command to run if no trace file provided |
| `--bug-id` | `BUG-LOCAL-1` | Identifier for this fix |
| `--no-git` | auto-detect | Force no-git mode |
| `--output-dir` | fresh temp dir | Where to write patch file and report (default: `{tmp}/sdlcma_out/{bug_id}_XXXX/`) |
| `--review` | off | Interactive review before applying (no-git mode) |
| `--config` | | Path to an agent-spec JSON (same shape as `configs/*.json`). When given, the standalone runner uses the first spec in the file and instantiates any `enhancements` declared on it (e.g. `configs/memory.json`). When omitted, runs a plain `LangGraphAgent` with no enhancements. |

### Code Inspection (standalone, independent of bug fix)

A separate code review agent that targets defects the bug-fix test oracle
structurally misses — found by reading code, not running tests (cross-module
contract mismatch, silent-empty/wrong-key, blind/unchecked index writes,
doc↔implementation drift, eval/state contamination). It is **not** an `Agent`
and does not depend on the bug-fix pipeline.

```bash
python -m inspection.standalone --path bf_worker/services/apply_patch.py
python -m inspection.standalone --path bf_worker/ --json report.json
python -m inspection.standalone --path X --fail-on high   # CI gate (exit 1)
```

| Flag | Default | Description |
|---|---|---|
| `--path` | (required) | File or directory to inspect |
| `--description` | | Optional free-text target description |
| `--json` | | Write the structured JSON report to this file |
| `--fail-on` | (never) | Exit non-zero if a finding at/above this severity exists (`high`/`medium`/`low`/`info`) |
| `--min-confidence` | `low` | With `--fail-on`, only count findings at/above this confidence (`high`/`medium`/`low`) — tolerant gate |

Quantify it against the known-defect set (real session defects across the
niche classes + correct same-domain negatives):

```bash
python -m inspection.acceptance   # → recall / precision / false-positive-rate
```

Phase 1 is standalone-only. The canonical acceptance fixture is the
pre-anchored `apply_change_infos` blind-write
(`inspection/fixtures/blind_apply_patch/`); `inspection/acceptance.py` is the
Phase-2 gate. The gate is the **stable fixture set** (recall/precision/FPR
1.00 across runs); 2 hardest boundary duals are accepted to flip ~20%
single-pass and tracked separately (`boundary_flaky` in their
`expected.json`), not counted in the gate. Each finding carries a
self-rated `confidence`. **Phase 2 is implemented**: the `code_review` graph
node runs the inspector on the test-passed patch (before commit) with a
config-selected **`mode`**:

- **shadow** (default) — only records its findings; **never affects the
  fix** (no feedback, no routing change; always proceeds to commit). Decouples
  measurement from intervention: the would-be value is recorded
  (`RunRecord.code_review_status` / `_finding_count` / `_would_escalate` /
  `_findings`, plus `code_review.json` in the journal) and evaluated offline
  at **zero fix-rate risk**. `fix_rate` is identical to baseline by
  construction.
- **acting** (explicit opt-in) — additionally feeds a high-severity &
  high-confidence finding back into the fixer for a **bounded** extra round
  (never blocks a green patch).

Opt-in via `LangGraphAgent(code_review=...)` (default OFF → pure pass-through,
baseline unchanged): `True` / `"kwargs": {"code_review": true}` → **shadow**;
`{"code_review": {"mode": "acting", "max_rounds": N}}` → **acting**.
`configs/code_review_vs_baseline.json` carries baseline + shadow + acting
specs. Enable acting only after a shadow run shows `would_escalate` is precise
enough. The "reviewer rescues a stuck fixer" enhancement remains planned —
see `/mnt/d/PL/sdlcma/code-review-agent-plan.md`.

### GitLab Mode

```bash
# 1. Gateway (webhook receiver)
uvicorn gateway.gateway:app --host 0.0.0.0 --port 8000

# 2. Orchestrator
python -m orchestrator.orchestrator
```

The GitLab worker can also consume an agent config through `BF_AGENT_CONFIG`.
When the first spec in that file declares `agent_ref`, the worker parent
process creates a temporary detached worktree at that branch/tag/commit and
re-executes `bf_worker/bf_worker.py` there. `BF_JOURNAL_DIR` is set to the
parent checkout's `evaluation/journal/` so the running-mode journal persists
after the temporary worktree is removed.

```bash
$env:BF_AGENT_CONFIG = "configs/baseline_last_commit.json"
python -m orchestrator.orchestrator
```

Or use `dh_entry.py` to launch both together:

```bash
python dh_entry.py
```

---

## Architecture

### Running Mode vs Evaluation Mode

```
┌─ Running mode ───────────────────────────────┐
│  picks ONE agent + config (the prod choice)  │
│  ├─ standalone submode (CLI, current code)   │
│  └─ gitlab submode (webhook, current code)   │
│  Side effects: real (MR / patch / commit)    │
│  Always-on journal: evaluation/journal/      │
└────────────────────┬─────────────────────────┘
                     │ both call agent.fix(BugInput)
┌────────────────────▼─────────────────────────┐
│   Agent layer  (bf_worker/agents/)           │
│   LangGraphAgent | (future) AiderAgent | ... │
└────────────────────┬─────────────────────────┘
┌────────────────────▼─────────────────────────┐
│ Evaluation mode  (evaluation/)               │
│  picks MANY agents × MANY fixtures           │
│  Output: evaluation/runs/<run_id>/, reports  │
│  Side effects: none (sandboxed providers)    │
└──────────────────────────────────────────────┘
```

### Agent Abstraction

The unit of comparison is the **agent**, not the graph. Different bug-fix approaches (our LangGraph state machine, third-party agents like Aider or SWE-agent, custom approaches) all implement the same minimal interface:

```python
class Agent(ABC):
    name: str
    def fix(self, bug_input: BugInput) -> FixOutput: ...
```

Adding a third-party agent means writing one adapter class — no need to refactor its internals into our graph.

| Agent | Description |
|---|---|
| `LangGraphAgent` | The default — wraps the LangGraph state machine + ReAct loop |
| (future) | Adapters for Aider, SWE-agent, or custom approaches |

`BugInput`, `FixOutput`, and `RunRecord` are the shared contracts. Per-agent enhancements live inside their owning agent — they do not pollute the shared interface.

### Hook System (LangGraphAgent extensions)

Per-LangGraphAgent enhancements (memory lookup, multi-hypothesis, edge-case test generation, …) plug in via a small `HookRegistry`:

```python
from enhancements.hooks import HookRegistry, HookName

def memory_lookup(state):
    # consult memory store, return dict to merge into state
    return {"prior_fixes": [...]}

agent = LangGraphAgent(enhancements=[(HookName.AGENT_PRE_FIX, memory_lookup)])
```

Currently wired hook points: `agent.pre_fix`, `agent.post_fix` (called from `LangGraphAgent.fix()`), `graph.pre_react_loop` (called from `graph/nodes/react_loop.py` — used by the memory enhancement to inject a `memory_hint` into the initial prompt), and `graph.post_apply_test` (called from `graph/nodes/apply_change_and_test.py` on every failure path — used by the reflection enhancement). Other graph-internal points (`graph.post_react_loop`, `graph.pre_apply_test`) are *named* but their call sites in the graph nodes are added when the first enhancement that needs them lands — adding hook calls without a concrete consumer would be premature.

#### Bundled enhancement: memory lookup

`bf_worker/enhancements/memory.py` is a token-overlap memory of past fixes. It registers a `PRE_REACT_LOOP` callback (queries `evaluation/memory/store.json` using `error_info` + `suspect_file_path` and injects up to `top_k` matches as `state["memory_hint"]`, which the ReAct prompt appends as a "Prior similar fixes (reference only)" section) and an `AGENT_POST_FIX` callback (appends each run's outcome to the store). The store is pre-seeded with 10 category-keyed lessons so the first sweep has something to retrieve. Compare baseline vs memory with `configs/memory_vs_baseline.json`.

#### Bundled enhancement: reflection

`bf_worker/enhancements/reflection.py` is opt-in self-reflection. It registers a `POST_APPLY_TEST` callback that fires whenever an apply+test cycle fails (and never on a green run). The callback has two lenses, picked by whether the patch ran: a **test-failure lens** (patch applied, tests failed) makes **one** LLM call producing a causal post-mortem — `WRONG_HYPOTHESIS` / `WHY_IT_FAILED` / `NEXT_FOCUS` (`reflection_mode="test"`); and an **apply-crash lens** (patch rejected, never applied) which emits a *deterministic* patch-mechanics note with **no LLM call** — because that failure is mechanical, not a reasoning error: the patcher is content-anchored (replaces the line equal to `original_line`, self-heals a stale `line_number`, rejects with `PatchAnchorError` rather than corrupting the file when it can't, and supports insertion by letting `new_line` span multiple lines spliced over the anchored line), so the remedy is a fixed contract, not causal analysis (`reflection_mode="apply"`). Both store `state["reflection_note"]`. Separately, the retry prompt now re-reads each touched file's **current on-disk content rendered with line numbers** as an authoritative block, so the LLM picks a valid `line_number` instead of anchoring against a stale file (the dominant retry failure observed in practice). This is deliberately *not* an "add more information" step: the raw pytest output is already fed back by the retry channel, and facing the same raw dump the LLM tends to re-derive the same wrong fix (pure resample). Reflection is a compression + causal re-framing over information already in hand, so `react_loop._format_retry_feedback` renders the post-mortem **first** and demotes the raw test output to an appendix beneath it. Without the enhancement the retry prompt is byte-identical to before. The reflection LLM call is accounted against the per-run budget and is hard-capped at `MAX_FIX_RETRIES` post-mortems per run (enforced inside the callback — the hook fires on every failure path including apply-crashes that don't advance the retry counter, so routing alone does not bound it). Compare baseline vs reflection with `configs/reflection_vs_baseline.json`.

Enhancements are translated from JSON spec entries (`{"kind": "memory", ...}` or `{"kind": "reflection"}`) into `(hook_name, callback)` tuples by `bf_worker/enhancements/build_enhancements.py:build_enhancements`. The same factory is used by both the evaluation runner (`evaluation/runner.py:make_agent`) and the running-mode entry points (`bf_worker/standalone.py` when `--config` is given, and GitLab workers when `BF_AGENT_CONFIG` is set), so the same agent spec file works across modes — e.g. `configs/memory.json` enables the memory enhancement on a single standalone run via `--config configs/memory.json`.

### RunRecord (canonical telemetry schema)

`bf_worker/agents/run_record.py` defines the `RunRecord` dataclass — the single source of truth for the structured outcome of one `agent.fix()` invocation. Both the running-mode journal and the evaluation runner write the same shape, so downstream tooling (metrics, promotion, dashboards) only handles one schema. Bump `SCHEMA_VERSION` for incompatible changes.

`RunRecord` includes platform result telemetry when providers return it:

- Agent code version: `agent_code_git_commit`, `agent_code_git_branch`, `agent_code_git_dirty`, `agent_code_git_status`
- Branch creation: `fix_branch_name`, `branch_create_status`, `base_branch`, `base_commit`, `branch_create_result`
- Commit/push: `commit_status`, `commit_branch`, `commit_hash`, `commit_result`
- Review output: `review_status`, `review_url`, `review_id`, `review_iid`, `review_branch`, `patch_file`, `report_file`, `review_result`
- Enhancement telemetry: `reflection_count` — how many reflection post-mortems the reflection enhancement produced this run; `reflection_mode` — the last lens used (`"apply"` deterministic patch-mechanics note | `"test"` LLM causal post-mortem | `None` when not wired / never fired). Additive and backward-compatible, so `SCHEMA_VERSION` stays `"1"`.
- LLM context telemetry: `max_input_tokens` — the largest `prompt_tokens` the backend reported across every LLM call this run (react_loop + reflection). `0` means the backend never returned a usage block; `None` means no LLM call ever ran. Useful for spotting when a run brushes a finite-window self-hosted backend's context limit. Additive, `SCHEMA_VERSION` unchanged.

GitLab runs populate commit and merge-request fields, local-git runs populate local commit fields, and no-git runs populate patch/report fields.

Project rule: when adding or changing `RunRecord` telemetry, update this documentation and the agent guidance files in the same change. Reproducibility depends on recording both the target repo state and the SDLCMA agent code version that produced the run.

### Journal & Evaluation

Every running-mode invocation writes a `RunRecord` to `evaluation/journal/<ts>_<bug_id>_<agent>_<model>/` — the model suffix lets you tell at a glance which LLM produced a given run, since model is a primary driver of bug-fix performance (slashes are slugified to dashes, length capped at 60). Auto-flagged candidates (failures, no-fix, high-iteration runs) can later be promoted into curated **fixtures** for the benchmark via `python -m evaluation.cli promote`.

```bash
python -m evaluation.cli list-fixtures                               # what's in the benchmark
python -m evaluation.cli list-journal --flagged                      # candidates worth promoting
python -m evaluation.cli promote <journal_entry> --category off-by-one
python -m evaluation.cli promote <journal_entry> --source-repo /path/or/url
python -m evaluation.cli run --config configs/baseline.json          # sweep configured agents × fixtures
python -m evaluation.cli run --fixture-id F01-off-by-one F03-missing-key  # subset
python -m evaluation.cli report <run_id>                             # comparison table
python -m evaluation.cli journal-prune --older-than 30d --keep-flagged  # dry-run retention
```

#### Published benchmark results

- [Cloud vs. self-hosted — initial benchmark (2026-05-24)](evaluation/reports/2026_05_24_cloud_vs_self_hosted.md) — first side-by-side of Dashscope `qwen3-coder-480b-a35b-instruct` vs. vLLM (+FlashInfer) `qwen2.5-coder-32b-instruct-awq` across all 19 bundled fixtures, plus a back-of-envelope FlashInfer effect estimate. Initial results, one sweep per backend — see the caveats section before quoting numbers.
- [LLM Gateway — two-cloud ladder (qwen3 + deepseek-v4-pro) (2026-05-25)](evaluation/reports/2026_05_25_gateway_qwen3_and_ds4.md) — first sweep through the new `llm_gateway` with the `qwen3_and_ds4.yaml` ordered policy, paired with the new no-fix retry edge (Plan A). **19/19 fix_rate** — first 100 % sweep on the bundled fixtures, with deepseek_v4_pro rescuing F10 on the apply+test retry path. Plan A's no_fix retry edge did not fire (qwen3 always produced a proposal at attempt=0); it remains the rescue path for the local-primary configuration.

The journal is always-on (override path with `BF_JOURNAL_DIR`); evaluation runs are sandboxed and never modify your real source. Evaluation also runs every cell with **checkpointing disabled** (`make_agent` sets `checkpointer=None`): the LangGraph checkpointer is keyed on `thread_id`, and in running mode `thread_id` defaults to `bug_id` (same bug across worker restarts = one thread = resume works). In evaluation `bug_id` is the *fixture id* — identical across every sweep, spec, and parallel process sharing one sqlite file — so leaving checkpointing on would let one cell silently resume another's state and corrupt the comparison. As defense-in-depth the runner also stamps `BugInput.thread_id = f"{fixture_id}::{spec_name}"` per cell, so even if a future change re-enables checkpointing in eval the keys can't collide between cells that use the same fixture under different agent specs. Never enable checkpointing for a sweep without first verifying this stays true; if results look impossible (a baseline cell with reflection telemetry, `test_passed` contradicting the trajectory), suspect a stale checkpoint. `list-journal --flagged` is only a review filter; `promote` can promote flagged or unflagged entries. Promotion tries to populate `fixtures/<id>/source/` automatically from the journal's buggy git commit (`base_commit`, falling back to `branch_create_result.commit`) and repo metadata (`project_web_url`, `source_repo_path`, or explicit `--source-repo`). If repo/commit information is missing, promotion still creates the fixture and leaves `source/` for manual population.

#### Journal retention (`bench journal-prune`)

The journal grows one directory per running-mode run and never self-cleans — by design, since interesting candidates are only known after the fact. For long-lived deployments, run `journal-prune` from cron / a systemd timer / a k8s `CronJob` to bound disk use:

```bash
python -m evaluation.cli journal-prune --older-than 30d --keep-flagged           # dry-run, prints the plan
python -m evaluation.cli journal-prune --older-than 30d --keep-flagged --apply   # actually delete
python -m evaluation.cli journal-prune --older-than 7d  --apply                  # delete every entry >7d, FLAGGED included
```

Behavior:

- **Dry-run is the default.** `--apply` is required to delete. The dry-run output lists what would go.
- **Time is read from the directory name**, not file mtime — `cp`/`rsync`/edits won't perturb retention.
- **Only directories matching the journal naming pattern (`YYYYMMDDTHHMMSSZ_…`) are eligible.** Hand-placed files / foreign directories are never touched.
- `--keep-flagged` protects entries with a `FLAGGED` marker (failed runs, no-fix, high-iteration runs) so promotion candidates aren't lost.
- `--journal-dir <path>` overrides the default `evaluation/journal/` (matches the runtime `BF_JOURNAL_DIR` override).

#### Bundled fixtures

10 single-file Python bugs in `evaluation/fixtures/` covering off-by-one, type-coercion, missing edge cases, recursion base case, mutable defaults, float precision, and string handling. Each fixture is self-contained (`source/` + `meta.json` + `requirements.txt`). See `evaluation/fixtures/F01-off-by-one/` for the canonical layout.

#### Configs

Agent specs live as JSON lists under `configs/`. The `baseline.json` config (no enhancements) is the reference point against which future enhancements are measured. Evaluation consumes every spec in the list; running-mode entry points consume the first spec. To compare approaches, write a config listing both, run, and compare — `configs/memory_vs_baseline.json` is a worked example:

Each agent spec may include optional `agent_ref` to pin that spec to a git branch, tag, or commit of the SDLCMA agent code. When omitted, null, or `"current"`, evaluation uses the current checkout in-process. When set, the evaluation coordinator creates a temporary detached git worktree at that ref, runs that subset of specs there, then copies the run records back into the parent `evaluation/runs/<run_id>/`. The same `agent_ref` field is honored by standalone and GitLab running-mode entry points for their first spec, using the same detached-worktree re-exec pattern. This lets one config compare agent behavior across code versions:

```json
[
  {"name": "baseline-current", "agent": "langgraph", "kwargs": {}},
  {"name": "baseline-old", "agent": "langgraph", "agent_ref": "a9d53e1172664d0bae05ed90b4196fd7f0f96827", "kwargs": {}}
]
```

```bash
python -m evaluation.cli run --config configs/baseline.json            # baseline only
python -m evaluation.cli run --config configs/memory_vs_baseline.json  # baseline + memory side by side
python -m evaluation.cli report run_<timestamp>
```

#### Remote evaluation against a self-hosted LLM (`infra/remote-eval/`)

For sweep runs against a model served by your own vLLM / llama.cpp / Ollama on a different host, `infra/remote-eval/deploy.sh` is the simplest path. It rsyncs source over SSH (Tailscale-friendly), bootstraps `.venv-linux` via `uv`, queries the remote's local `http://127.0.0.1:8000/v1/models` to discover the served model id, and writes `settings/worker_local_multi_process.env` pointing the worker at `127.0.0.1:8000`. Reuses the existing `local_multi_process` ENV — the discriminator for "self-hosted backend" lives in the three LLM fields, not in a new ENV name. `LLM_API_KEY` defaults to `"EMPTY"` so you do not have to invent a fake key.

```bash
# Defaults: HOST=100.81.178.68 SSH_KEY=~/.ssh/ls4090 SSH_USER=ls REMOTE_DIR=/home/ls/sdlcma
bash infra/remote-eval/deploy.sh

# Then on the remote:
ssh -i ~/.ssh/ls4090 ls@100.81.178.68
cd ~/sdlcma && source .venv-linux/bin/activate
uv run python -m evaluation.cli run --config configs/baseline.json

# Pull results back:
rsync -az -e "ssh -i ~/.ssh/ls4090" \
  ls@100.81.178.68:~/sdlcma/evaluation/runs/ ./evaluation/runs/
```

The harness is **eval-only**: no gateway, orchestrator, or Redis. First run is full sync + venv + `uv pip install -r requirements.txt`; subsequent runs are pure incremental rsync. The new `max_input_tokens` RunRecord field surfaces how close each cell got to the backend's context limit.

**Tool-calling fallback for self-hosted backends.** vLLM + Qwen2.5-Coder-Instruct (and similar combos where chat-template and tool-call parser disagree on the wrapper tag) sometimes returns a valid tool-call JSON in the assistant message's `content` while leaving `tool_calls` empty. `react_loop._maybe_recover_tool_call_from_content` recovers it — bare JSON, `<tool_call>…</tool_call>`, `<tools>…</tools>`, or a ```json``` fence are all accepted. Strict no-op on Dashscope / OpenAI / vLLM with a matched parser+template. If you do start vLLM cleanly, the recommended Qwen2.5-Coder setup is `--enable-auto-tool-choice --tool-call-parser hermes --chat-template <vllm-repo>/examples/tool_chat_template_hermes.jinja`.

#### MCP server (`mcp_server/`)

The same evaluation state — fixtures, journal entries, sweep runs — is also
exposed over the [Model Context Protocol](https://modelcontextprotocol.io)
so an MCP-aware client (Claude Desktop, mcp-cli, an MCP-aware agent) can
introspect and act on it. Read-only tools: `list_fixtures`,
`read_fixture`, `list_journal_entries`, `read_journal_entry`,
`list_eval_runs`. One write tool: `promote_journal_to_fixture` (delegates
to `bench promote`). Two resources: `sdlcma://fixtures` and
`sdlcma://runs/{run_id}/summary`.

```bash
# Run the server over stdio (what Claude Desktop and mcp-cli speak)
uv run python -m mcp_server.server
```

Setup for Claude Desktop and design notes in [`mcp_server/README.md`](mcp_server/README.md).

### Provider Abstraction

The worker's LangGraph nodes access all external resources through a **provider abstraction layer** (`bf_worker/providers/`). This decouples the core bug-fixing logic from any specific platform:

```
                    ┌───────────────────────┐
                    │   LangGraph Nodes     │
                    │  (platform-agnostic)  │
                    └───────────┬───────────┘
                                │ state["provider"]
                    ┌───────────▼───────────┐
                    │   Provider ABCs       │
                    │  Source / VCS / Review │
                    └───┬───────┬───────┬───┘
                        │       │       │
               ┌────────▼┐ ┌───▼────┐ ┌▼─────────┐
               │ GitLab  │ │ Local  │ │ LocalNo   │
               │ Provider│ │ Git    │ │ Git       │
               └─────────┘ └────────┘ └───────────┘
```

| ABC | Responsibility |
|---|---|
| `SourceProvider` | Fetch CI traces and source file content |
| `VCSProvider` | Repo setup, branch creation, commit/push |
| `ReviewProvider` | Post-fix output (MR, CI wait, report) |

### Services (GitLab Mode)

Three independently-running services communicate via **Redis Streams**:

| Service | Role |
|---|---|
| **Gateway** | Stateless FastAPI app. Receives GitLab webhooks and writes them to `gateway:stream`. |
| **Orchestrator** | Async event loop. Reads the stream, spawns one Worker subprocess per bug, monitors heartbeats, routes validation results back to workers. |
| **Worker** | Spawned once per bug. Runs the LangGraph fix pipeline, maintains a Redis heartbeat, cleans up on exit. |

### Worker Graph

The same LangGraph state machine runs in all modes:

```
fetch_trace ↺ → parse_trace → fetch_source_file ↺ → react_loop
                          ↓                                  ↑
                          └──(parser found no path)──────────┘
                             (skip fetch_source_file)
    → create_fix_branch → apply_change_and_test → commit_change ↺
    → wait_ci_result ↺ → create_mr ↺ ─────────────→ END
                                                     ↑
      [any node failure above] ──→ handle_failure ───┘
```

(↺ = the node wraps its provider call in the shared narrow transient-retry layer in `services/transient_retry.py` — up to 2 retries with `(1s, 2s)` backoff on known-transient classes, `Retry-After` honored on 429/5xx, permanent errors propagate immediately.)

(`fetch_source_file` always advances structurally to `react_loop`; on read failure it returns empty `source_file_content` plus `source_fetch_failed=True`, and the `react_loop` prompt branches into a fallback shape that surfaces the parser's path as a hint.)

The **ReAct loop** gives the LLM tools (`fetch_additional_file`, `fetch_file_segment`, `submit_fix`, `abort_fix`) and runs up to 8 reasoning steps. The patch is applied and tested in an isolated Python venv before being committed.

When a fix fails its tests, the loop is re-entered (up to `MAX_FIX_RETRIES=2` times). On each retry the next prompt carries forward the previous attempt's patch, `apply_error`, and the tail of pytest's output (`test_output`, truncated to 4000 chars) — each wrapped in UNTRUSTED delimiters by `prompt_guard` so pytest output cannot hijack the LLM through the retry channel. Without this feedback channel a retry would simply resample the same prompt and likely produce the same wrong fix.

Three recoverable failure modes that used to abort the run now keep it alive — the first via a narrow retry on the network/I-O call itself, the other two via fallback into `react_loop` with the raw trace:

- **Transient I/O retry across all five I/O-bound nodes** — `fetch_trace`, `fetch_source_file`, `commit_change`, `wait_ci_result`, and `create_mr` each wrap their provider call in the shared `services.transient_retry.with_transient_retry()` helper. Policy: up to 2 retries, `(1s, 2s)` backoff. Transient classes: HTTP `ConnectionError` / `Timeout` / `ChunkedEncodingError`, `HTTPError` 5xx, `HTTPError` 429 (with `Retry-After` honored, clamped at 30s), `OSError` with errno in `{EAGAIN, EBUSY, EIO, ENFILE, EMFILE, ENOMEM, ETIMEDOUT}`, `redis.exceptions.ConnectionError` / `TimeoutError` / `BusyLoadingError`. Everything else — 4xx other than 429, `FileNotFoundError`, `PermissionError`, `redis.ResponseError`, unrelated exceptions — propagates immediately so misconfiguration surfaces fast. Each node records its own retry counter (`fetch_trace_retries`, `fetch_source_file_retries`, `commit_change_retries`, `wait_ci_result_retries`, `create_mr_retries`) into `RunRecord` for cost telemetry. `fetch_source_file` additionally falls through to `source_fetch_failed=True` on exhausted transients; the other three propagate.



- **`parse_trace_fallback`** — the regex parser in `parse_trace` couldn't extract both a structured error and a `<path>.py:<line>` reference (unusual traceback format, plain log output, path the regex missed). The node forwards the tail of the raw trace (capped at 8000 chars) as `error_info`, leaves `suspect_file_path=""`, and sets `parse_trace_fallback=True`. The graph skips `fetch_source_file` and goes directly to `react_loop`. Empty / whitespace-only traces still hard-fail.
- **`source_fetch_failed`** — the parser produced a path but `provider.fetch_file` raised (synthetic frame like `<frozen importlib._bootstrap>`, file moved/renamed since the trace, path outside the working tree, encoding error). `fetch_source_file` returns `source_file_content=""`, `source_fetch_failed=True`, and keeps `suspect_file_path` populated so the LLM gets the parser's path as a starting hint.

In either mode the LLM works from the raw trace and uses `fetch_additional_file` to find the right file, and every fix entry must set `file_path` explicitly — `apply_change_and_test` rejects entries that omit it via the existing `apply_error` channel. Known limitation: there is no directory-listing tool, so the fallback is only effective when the trace itself mentions a usable path.

### Security & Guardrails

DevHarness runs an autonomous LLM with write authority over your working tree, so a hallucinated path or prompt-injected trace could in principle target a sensitive file. To bound that blast radius, every patch is validated by `bf_worker/services/patch_guard.py` *before* anything is written to disk.

`validate_patch_scope` rejects a fix when:

- The target path resolves outside the repo root (after symlink resolution) — blocks `..` traversal, absolute paths, and symlink escapes.
- The repo-relative path matches a sensitive deny glob — `.env*`, `*.env`, `.git/**`, `.ssh/**`, `id_rsa*`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, `.aws/**`, `.gnupg/**`, `*credentials*`, `*secrets*`.
- The patch exceeds the per-run caps (default `max_files=5`, `max_lines=50`).

A rejection raises `PatchScopeError`, surfaces as `apply_error` / `test_output: [patch_guard rejected]`, and counts toward `MAX_FIX_RETRIES`. The original source is never mutated and the LLM gets the rejection message back on retry. Unit tests live in `tests/test_patch_guard.py`.

This is **blast-radius defense** — it stops a bad write but does not, on its own, prevent the LLM from being talked into proposing one. The next layer covers that.

#### Prompt-injection defense (`bf_worker/services/prompt_guard.py`)

Everything the LLM reads at runtime — CI traces, suspect-file content, files fetched mid-loop, and the memory hint — is untrusted. A malicious comment in source code, a poisoned trace line, or a tampered memory entry could try to talk the LLM into ignoring its system prompt. Three layers defend against that:

1. **System-prompt hardening.** A `[SECURITY]` paragraph in the ReAct system message states that all CI/file content is data, never instructions, and that the only valid actions are calling `fetch_additional_file`, `fetch_file_segment`, `submit_fix`, or `abort_fix`.
2. **Untrusted-content delimiters.** `wrap_untrusted()` wraps every untrusted block in `<<<UNTRUSTED:label>>> … <<<END UNTRUSTED:label>>>` markers, so the LLM has a clear boundary between task instructions and material to analyse. Used in `_build_initial_messages` and in every tool-fetch result.
3. **Pattern detection (log-only).** `detect_injection()` scans for common markers — ignore-prior-instructions phrasing, chat-template tokens (`<|im_start|>`, …), fake `<tool_call>` tags, role-forging at line start — and logs each hit. Detection is intentionally not a block: legitimate code can contain such strings (e.g. tests for prompt-injection defenses themselves).

`prompt_guard` and `patch_guard` are complementary: the prompt guard tries to keep the LLM on task in the first place; the patch guard catches the bad write if it happens anyway. Unit tests in `tests/test_prompt_guard.py`.

#### Fetch-path containment (`bf_worker/services/fetch_guard.py`)

The mirror of `patch_guard` for the *read* path. The LLM's `fetch_additional_file` and `fetch_file_segment` tools accept a path argument; without validation, a hijacked LLM could ask for `/etc/passwd`, `.env`, or a path that traverses out of the repo, and the provider would happily return its content. The LLM could then leak that content via `error_reason` or stash it inside a patch.

`validate_fetch_path` runs before the provider is touched and rejects:

- empty paths, absolute paths, and any path containing a `..` segment,
- repo-relative paths that match the same sensitive denylist `patch_guard` uses (`.env*`, `.git/**`, `.ssh/**`, `*.pem`, `*.key`, `*credentials*`, `*secrets*`, …) — re-exported from `patch_guard.DENY_GLOBS` so the read and write surfaces share one source of truth.

On rejection, the tool returns `[fetch rejected: <reason>]` to the LLM, which sees the error on the next loop turn and can revise. Tests in `tests/test_fetch_guard.py`.

#### Side-effect idempotency (branches / commits / MRs)

When a worker dies mid-run (network blip, OOM, redeploy) the orchestrator restarts it. Without idempotency, the second run would create a *second* branch, a *second* MR, and possibly push a duplicate commit — every restart bleeds noise into the project's history. The provider layer guarantees no duplicate side-effects across restarts:

**Deterministic branch naming.** `auto/bf/{bug_id}-{base_commit[:8]}` — same bug, same base commit → same branch name. If the user's main branch advances, the dedup key changes, and a fresh branch is born. That's the right semantic, not a bug.

**Three-state push** (`commit_status`):

| Remote state | Action | `commit_status` |
|---|---|---|
| Branch absent on origin | regular push | `success` |
| Remote tree == local tree (same content already there) | no push, no-op | `reused` |
| Remote is ancestor of local (we'd fast-forward) | regular push | `success` |
| Diverged history (stale fix on remote ≠ our fix) | force-push (overwrite) | `updated` |

Equality is computed at the *git tree* level (`commit^{tree}`), so commit metadata differences (timestamp, author) don't trip the `reused` path. Divergence is detected via `git merge-base --is-ancestor`, not fragile `HEAD~` arithmetic.

**MR lookup-then-create** (`review_status`):

| Existing MR for branch | Action | `review_status` |
|---|---|---|
| open | return existing | `reused` |
| merged | return existing, signal R10 short-circuit | `already_merged` |
| closed | open new MR (closed = previous attempt rejected) | `opened` |
| none | open new MR | `opened` |
| 409 from POST (concurrent worker raced us) | re-lookup, return existing | `reused` |

**R10 short-circuit (early).** A `precheck_already_fixed` node runs *before* `fetch_trace` — a single REST call (no clone, no LLM) that asks GitLab whether any merged MR exists for `auto/bf/{bug_id}-*`. If yes, the graph routes directly to `END` with `outcome="already_fixed"`. This saves the entire fetch + parse + ReAct loop's LLM cost when the fix has already shipped. A second (defensive) check inside `create_fix_branch` exact-matches the deterministic branch and short-circuits the same way if the precheck missed it (e.g. the merge happened mid-run).

**Trade-off — the MR is a moving target.** Force-push (`updated`) rewrites the source branch when a previous run left a wrong fix. Reviewers' line-anchored comments on the open MR will become outdated when this happens. This matches the convention used by Renovate, Dependabot, and similar auto-fix bots, and is the right semantic for "the bot's current best attempt."

**Out of scope: field-level correctness.** This implementation guarantees *no duplicate side-effects*, not *field-level correctness of pre-existing MRs*. If an existing MR has a stale title/body/labels because the bot's template changed between runs, we leave them alone — repairing those belongs to a separate "MR refresh" feature, not idempotency.

Tests: `tests/test_idempotency.py` covers ten rainy-case scenarios (R1–R10) with a real local git "origin" and an in-memory MR registry — including the 409 race, force-push failure, and tree-equality reuse.

#### Resume after crash (LangGraph checkpointing)

When a worker dies mid-run (HealthMonitor expiry, OOM, deploy), the orchestrator restarts it. Without persistence, the new process re-runs `precheck → fetch_trace → react_loop` and **re-spends the LLM tokens** that were already burned. The checkpointer fixes that: every node-boundary state update is persisted; restart resumes at the next un-completed node.

**Architectural relationship to idempotency** (they're complementary, not redundant):

- **Idempotency layer** (provider) = *correctness*. Even if checkpoint is wrong / lost / a node ran but checkpoint write failed, the next run's side-effects are caught by the dedup logic and don't produce duplicates.
- **Checkpointer** = *cost*. It skips the work already done, but trusts itself to do so. Without idempotency, a wrong checkpoint could cause data corruption.

You want both. Checkpointer says "skip"; idempotency says "if you don't skip, do it safely."

**Backend selection** (env var `BF_CHECKPOINT_BACKEND`):

| Value | Use case | Storage |
|---|---|---|
| `sqlite` (default) | Single-host deployments, standalone mode | File at `~/.sdlcma/checkpoints/state.sqlite` (override via `BF_CHECKPOINT_PATH`) |
| `redis` | Multi-host / centralized inspection | The same Redis the orchestrator uses (`redis_url`); needs `langgraph-checkpoint-redis` installed |
| `memory` | Tests only | In-process; lost on exit |
| `none` | Opt-out | No checkpoint; pre-checkpoint behavior |

**Thread ID**: keyed on `bug_id` (same as the idempotency dedup key). Same bug across restarts shares one thread → resume works. Different bugs are independent.

**Schema evolution**: when you change the graph (add/rename nodes, change `BugFixState` shape), old checkpoints become stale. Current behavior is fail-fast: an invalid resume raises rather than silently skipping. Operationally, bumping a graph node should pair with `rm ~/.sdlcma/checkpoints/state.sqlite` (or the equivalent for Redis).

**State vs config**: `provider`, `hooks`, and `budget` are NOT in checkpointed state — they live in `config["configurable"]` (`bf_worker/services/runtime_context.py`). LangGraph passes config to nodes but doesn't persist it. This is what allows resume across processes: a fresh process supplies a fresh provider, and the checkpoint state has no stale connection handles to deserialize.

**Trade-off — wallclock budget on resume**: when a run resumes, the `RunBudget` is fresh (its `wallclock_s` resets). The justification is that resume only happens after a non-graceful exit; the spent wallclock from the killed process is unrecoverable. If you need stricter accounting, override `BF_CHECKPOINT_BACKEND=none` or add a checkpoint-aware budget.

Tests: `tests/test_checkpointer.py` covers the resume-from-crash semantics, thread isolation, and the property that provider isn't persisted across runs.

#### Run budget (`bf_worker/services/budget.py`)

A per-`agent.fix()` hard cap on three dimensions, so a hijacked or pathological run cannot rack up unbounded cost:

| Dimension | Default | Why |
|---|---|---|
| LLM calls | 30 | One honest fix uses 2–8; 30 covers retries with headroom. |
| Total tokens | 200 000 | One honest fix uses 5–20k; 200k catches runaway loops. |
| Wall-clock seconds | 300 | One honest fix is well under a minute; 5 min is the abort line. |

`RunBudget` is instantiated in `LangGraphAgent.fix()` and threaded into `state["budget"]`. `react_loop` calls `budget.check()` before every LLM call (skips and ends the loop with `llm_result=None` if exhausted) and `budget.record_call(input_tokens, output_tokens)` after, using LangChain's `usage_metadata`. The exhaustion reason is logged and surfaced in the run record. Tests in `tests/test_budget.py`.

---

## Requirements

- Python 3.10+
- An OpenAI-compatible LLM API (tested with Alibaba Dashscope / Qwen)

Additional for GitLab mode:
- Redis 7+
- GitLab instance with webhook support

---

## Installation

```bash
git clone <repo-url>
cd devharness

# Using uv (recommended)
uv pip install -r requirements.txt

# Or pip
pip install -r requirements.txt
```

---

## Configuration

DevHarness uses a two-step config loading pattern:

1. `settings/.env` declares the active environment name (e.g. `ENV=local_multi_process`)
2. Each service loads its own `<service>_<ENV>.env` file for actual settings

Copy the example files and fill in your values:

```bash
cp settings/.env.example                             settings/.env
cp settings/orchestrator_local_multi_process.env.example  settings/orchestrator_local_multi_process.env
cp settings/worker_local_multi_process.env.example        settings/worker_local_multi_process.env
cp gateway/gateway_local_multi_process.env.example        gateway/gateway_local_multi_process.env
```

### Sensitive fields (required in worker env file)

| Variable | Description |
|---|---|
| `GITLAB_PRIVATE_TOKEN` | GitLab personal access token with `api` scope (GitLab mode only) |
| `LLM_API_KEY` | API key for your LLM provider. Defaults to `"EMPTY"` (the vLLM-community convention) when omitted, so self-hosted backends (vLLM, llama.cpp's server, Ollama) work without it. Cloud backends still require a real key. |
| `LLM_REQUEST_TIMEOUT` | Optional. Per-LLM-call HTTP timeout in seconds. Default 600 — sized for self-hosted CoT-heavy Qwen-style models on a single GPU, where one step can take minutes. Cloud backends should override down (e.g. `LLM_REQUEST_TIMEOUT=60`). |
| `LLM_API_BASE_URL` | OpenAI-compatible base URL (e.g. Dashscope, or an SDLCMA `llm-gateway` instance) |
| `LLM_MODEL` | Model name (e.g. `qwen3-coder-480b-a35b-instruct`). When `LLM_VIA_GATEWAY=true` this is informational — the gateway rewrites `model` to the selected backend's declared name. |
| `LLM_VIA_GATEWAY` | Optional. Set `true` when `LLM_API_BASE_URL` points at an SDLCMA `llm-gateway` (orthogonal opt-in). Worker attaches `X-Sdlcma-Bug-Id` + `X-Sdlcma-Attempt` hint headers to every LLM call and skips the self-hosted startup model-name probe. Default `false` = byte-identical to the pre-gateway path. See "LLM Gateway" below. |

### LLM Gateway (optional, opt-in)

`llm_gateway/` is an independent FastAPI service that routes OpenAI-compatible requests to one of N configured backends per a **stateless inference policy** (today: ordered ladder — the worker's `X-Sdlcma-Attempt` header is treated as a difficulty coefficient, attempt=0 picks the primary backend and each retry climbs one rung). It is fully orthogonal to the deployment mode below — turn it on for any deployment, or leave it off; the worker code path is byte-identical with it off.

Three bundled configs in `configs/llm_gateway/`:

| File | Backends | Use case |
|---|---|---|
| `qwen3_api.yaml` | Dashscope only | Cloud-only; gateway in the path for telemetry / future multi-worker fan-out |
| `self_hosted.yaml` | local vLLM only | All self-hosted |
| `qwen3_plus_local.yaml` | Dashscope primary + local vLLM fallback | Try cloud first, escalate to self-hosted on retry or cloud outage |

Switching it on:

```bash
docker compose \
  -f docker-compose.yml -f docker-compose.llm-gateway.yml \
  --profile build build
docker compose \
  -f docker-compose.yml -f docker-compose.llm-gateway.yml up -d
# In the worker env file:
# LLM_API_BASE_URL=http://llm-gateway:9000/v1
# LLM_VIA_GATEWAY=true
```

Switching it off (transition / disable):

```bash
# Don't pass -f docker-compose.llm-gateway.yml; don't set LLM_VIA_GATEWAY.
# Worker keeps talking to whatever upstream LLM_API_BASE_URL points at.
```

The gateway records the chosen backend in `RunRecord.llm_backend_name` (additive — `SCHEMA_VERSION` unchanged) so per-backend evaluation aggregation works even when several backends serve the same model name. Full contract in `docs/architecture.md` ("LLM Gateway" section).

---

## Deployment Methods for GitLab Running Mode

In GitLab mode, DevHarness can be deployed in six ways, controlled by `settings/.env` and (for spawners that diverge from the historical by-env default) the additive `WORKER_SPAWNER` setting:

### Mode 1: Local Multi-Process (`ENV=local_multi_process`)

Services run as separate processes on the host. Workers are spawned as subprocesses by the orchestrator.

```bash
# 1. Gateway (webhook receiver)
uvicorn gateway.gateway:app --host 0.0.0.0 --port 8000

# 2. Orchestrator
python -m orchestrator.orchestrator
```

### Mode 2: Docker Compose (`ENV=local_docker_compose`)

Gateway, Orchestrator, and Redis run as Docker containers. Workers are spawned as separate containers on demand by the orchestrator via the Docker API.

**Prerequisites:**
- An external Docker network `sdlcma_net` shared with the GitLab compose stack
- SSH private key configured in `settings/orchestrator_local_docker_compose.env`

```bash
docker network create sdlcma_net
docker compose build
docker build -f Dockerfile.bf-worker -t dh-bf-worker:latest .
docker compose up
```

### Mode 3: Docker Compose over HTTP, no SSH (`ENV=local_docker_compose_http`)

Same containerized topology as Mode 2 (Gateway/Orchestrator/Redis as
containers, per-bug worker spawned as a container via the Docker socket), but
clone **and** push go over `http://<user>:<token>@gitlab/...` with **no SSH**
— the additive sibling of Mode 2 that mirrors the `gitlab_saas` no-SSH model
over plain HTTP. It is **Stage 1 of the cloud rehearsal**: prove the
containerization itself locally before pointing the same stack at gitlab.com
(`gitlab_saas` + cloudflared). No SSH key required; spawned worker containers
run with `BF_CHECKPOINT_BACKEND=none` (ephemeral). Mutually exclusive with any
other orchestrator on the same Redis (see `tests/TESTING.md`).

```bash
docker network create sdlcma_net                 # if absent
docker network connect sdlcma_net gitlab         # GitLab container reachable as `gitlab`
docker compose --profile build build             # builds gateway/orchestrator/worker
docker compose up -d                             # ENV=local_docker_compose_http
# point the project webhook at  http://gateway:8000/webhook
```

Full Stage-1/Stage-2 plan and the containerization gaps it caught:
`docs/deployment.md` + `/mnt/d/PL/sdlcma/cloud-gitlab-plan.md`.

### Mode 4: gitlab.com via cloudflared (`ENV=gitlab_saas`)

Same containerized topology as Mode 3 (Gateway/Orchestrator/Redis + per-bug
worker container), but the GitLab target is **gitlab.com**, not a self-hosted
GitLab. This is the natural next step after Mode 3: the wiring rehearsed
there (token/HTTP, no SSH) now points at the public SaaS.

- HTTPS + `oauth2:<token>` for clone and push (no SSH, no host rewrite).
- `WORKER_SPAWNER=docker` makes the orchestrator spawn workers as containers
  in this containerized topology. (`WORKER_SPAWNER` is the additive seam
  that decouples *where the worker runs* from *which GitLab* — leave it
  unset on the AWS single-host systemd variant, see below.)
- Inbound webhook reaches the gateway through a **cloudflared quick tunnel**:
  gitlab.com posts to `https://<assigned>.trycloudflare.com/webhook`, and
  cloudflared dials *out* and pushes the request to `gateway:8000` inside
  `sdlcma_net`. No DNS, no TLS, no inbound port to open.

**Why cloudflared — convenience, not necessity.** If the host has a public
IP with inbound `:8000` open, or terminates HTTPS in front of the gateway
(e.g. a reverse proxy on `:443`), gitlab.com can post directly and the
tunnel is unnecessary. We chose cloudflared because Phase 0.5 ran on an
IONOS host whose provider firewall only admits 22/80/443 — so even with a
public IP we could not expose `:8000` directly — and because the quick
tunnel needs no DNS or TLS provisioning, which is the fastest path for
testing.

```bash
docker network create sdlcma_net                 # if absent
docker compose --profile build build
docker compose up -d                             # ENV=gitlab_saas
# read the public URL from the cloudflared container's logs and configure
# the gitlab.com project webhook to:
#   https://<assigned>.trycloudflare.com/webhook
```

A single-host **systemd** variant on the same `gitlab_saas` env (no
containers, subprocess spawner — leave `WORKER_SPAWNER` unset) is the AWS
target; harness lives in `infra/aws-gitlab/`. Reboot-OFF semantic and the
co-tenant rules with other services on the same public host are in
`docs/deployment.md`.

### Mode 5: AWS ECS (`ENV=gitlab_saas` + `WORKER_SPAWNER=ecs`)

Managed-cluster variant of Mode 4, on a free-tier `t3.micro` EC2 instance.
The four long-running services (redis + gateway + orchestrator +
cloudflared) run as **one ECS Service** in `host` network mode, sharing the
host's primary ENI's public IP. Per-bug `bf-worker` is launched as a
**one-off ECS task** via `ecs:RunTask` (also `host` network mode — the
secondary `awsvpc` ENI in a public subnet does NOT auto-assign a public IP
and would need a NAT Gateway).

- The worker reuses the `gitlab_saas` GitLab-provider branch unchanged.
  AWS ECS is purely a *spawner swap*, controlled by `WORKER_SPAWNER=ecs`
  in `settings/orchestrator_ecs.env` (additive; existing envs/tests are
  byte-identical).
- Single-file infrastructure: `infra/aws-ecs/stack.yml` (CloudFormation).
  3× ECR repos + 2× IAM roles + 2× security groups + 1× EC2 + 1× ECS
  cluster + 2× task definitions (services + bf-worker) + 1× ECS Service.
- Free tier: `t3.micro` 750 hrs/mo + 8 GB EBS + 5 GB CloudWatch ingest
  + 500 MB ECR. No NAT Gateway, no ALB, no EKS.

```bash
# 1. Create the CloudFormation stack
KEY_NAME=my-ec2-key \
GITLAB_TOKEN=glpat-... \
LLM_API_KEY=sk-... \
bash infra/aws-ecs/create-stack.sh

# 2. Build and push the 3 images to ECR
bash infra/aws-ecs/deploy-images.sh

# 3. Scale the service up + grab the cloudflared URL
aws ecs update-service --cluster sdlcma-cluster --service sdlcma-services \
                       --desired-count 1 --region eu-north-1
aws logs tail /sdlcma/services --filter trycloudflare --region eu-north-1
# → https://<assigned>.trycloudflare.com  (set as the gitlab.com webhook)
```

Verified end-to-end (2026-05-21): `lishu20161/order_be` failing pipeline →
trycloudflare URL → ECS services task → orchestrator → `ecs:RunTask` →
bf-worker task → fix branch pushed → fix-branch CI success → validation
event routed back via Redis → MR opened on gitlab.com. Trigger → MR ≈ 65 s.
Full runbook + 9 deployment-specific gotchas (assignPublicIp Fargate-only,
secondary-ENI public-IP behavior, services-task `MemoryReservation` vs
`Memory`, single-instance `MinimumHealthyPercent`, etc.) in
`docs/deployment.md`. Teardown via `bash infra/aws-ecs/delete-stack.sh`
(add `DELETE_ECR=1` to also drop ECR repos).

### Mode 6: Kubernetes / kind (`ENV=gitlab_saas` + `WORKER_SPAWNER=k8s`)

Local-cluster variant of Modes 4/5: the existing Helm chart in
`infra/helm/sdlcma/` running on a single-node `kind` cluster, with
**cloudflared** providing inbound webhook ingress (no public IP required).
Per-bug workers are launched as **`bf-worker-<slug>` Jobs** via the
in-cluster BatchV1Api by `K8sJobSpawner` (`backoff_limit=0` — the
orchestrator's HealthMonitor owns retries; `ttlSecondsAfterFinished` self-GC;
`automount_service_account_token=False`; ephemeral
`BF_CHECKPOINT_BACKEND=none` per invariant #4).

- Same spawner-decoupling pattern as Mode 5: the worker reuses
  `gitlab_saas` unchanged and `WORKER_SPAWNER=k8s` is layered on via the
  Helm overlay `infra/helm/sdlcma/values-gitlab-saas.yaml`.
- Images are `kind load`-ed (no registry); chart sets
  `imagePullPolicy: IfNotPresent`.
- cloudflared is a dedicated Deployment dialing OUT to Cloudflare's edge
  for a quick `trycloudflare.com` URL — new URL on each pod restart; use
  a named tunnel + credentials Secret if you need stability.
- **ingress-nginx (additive; step 4b of `setup.sh`, default ON, skip with
  `INSTALL_INGRESS_NGINX=0`)** — closes the chain that the chart's
  existing `Ingress` resource (`ingress.enabled=true` by default) and the
  kind cluster's `extraPortMappings :18080→:80` already half-wire,
  exposing the gateway webhook on the host's `:18080` (and any name/IP
  that routes to the host — tailnet, LAN, public hostname) without
  cloudflared. Pick cloudflared when GitLab has no direct route to this
  host; pick the ingress path when it does (tailnet/LAN/hostname). Both
  paths can coexist; to turn cloudflared off on a host that uses the
  ingress path only, drop `cloudflared: { enabled: false }` into
  `infra/helm/sdlcma/values.local.yaml` (gitignored; auto-layered by
  `setup.sh` on top of the tracked overlay). CN-network gotcha: the upstream manifest pins
  `controller` + `kube-webhook-certgen` images by `@sha256` digest at
  `registry.k8s.io`, so `kind load` + retag is silently insufficient
  (kubelet resolves the digest at the original URL). `setup.sh` rewrites
  `registry.k8s.io/` → `m.daocloud.io/registry.k8s.io/` in the manifest
  before applying — override the prefix with `INGRESS_NGINX_PROXY=""` to
  hit upstream directly on non-CN networks. Verified 2026-05-27 on a CN
  host over a Tailscale tailnet → `curl http://<tailscale-ip>:18080/healthz`
  returns 200.
- Bare-host prereqs (codified in `infra/k8s/setup.sh`): cgroup v2 must be
  enabled (`systemd.unified_cgroup_hierarchy=1` in grub on Ubuntu ≤21.04
  — k8s 1.35 kubelet refuses cgroup v1), swap off, and the bf-worker host
  needs to be able to docker-pull `redis:7-alpine` + `cloudflare/cloudflared`
  before kind-loading them (the kind worker's containerd does NOT inherit
  the host's docker `registry-mirrors`).
- Concurrent-worker scaling: verified 2026-05-27 on a fresh bare-Ubuntu
  host (56 CPU / 62 GiB) with 7 truly-simultaneous workers (N=8 strict
  burst via `tools/trigger_concurrent_pipelines.py --concurrency 8`,
  one bug_id-collision dedup was a known orchestrator race when many
  webhooks land within the same wall-clock second; closed by the
  urandom 4-hex tail now appended to every orchestrator-minted bug_id)
  — 0 HealthMonitor false-positive restarts, all spawned workers
  reached `outcome=fixed` except where the LLM itself produced a bad
  patch.

```bash
# 1. populate settings/worker_gitlab_saas.env with GITLAB_PRIVATE_TOKEN + LLM_API_KEY
# 2. bring up the stack (kind + build + load + helm install + URL extract)
bash infra/k8s/setup.sh
# 3. configure the gitlab.com project webhook to the printed URL + /webhook
# 4. trigger a failing pipeline, or run the smoke
PROJECT_PATH=user/repo bash infra/k8s/gitlab-smoke.sh
# 5. teardown
bash infra/k8s/teardown.sh
```

For multi-repo concurrent stress (N independent fixtures rather than
re-triggering one repo), use the bundled tools:

```bash
# create N gitlab.com repos, each with one bundled fixture as the buggy main
uv run python tools/gitlab_fixture_repos.py --fixtures F01,F02,F03,F04 \
  setup --webhook-url https://<cloudflared>.trycloudflare.com/webhook

# fire N pipelines truly simultaneously — pass --namespace/--fixtures so
# the script can scope the project search (the trigger script no longer
# uses owned=true, which is unreliable for the root admin token on
# self-hosted Omnibus); pick --concurrency == len(fixtures) for a strict
# burst rather than a thread-pool-paced trickle
uv run python tools/trigger_concurrent_pipelines.py \
  --fixtures F01,F02,F03,F04 --concurrency 4
```

Full runbook in `infra/k8s/README.md`.

### GitLab Webhook Setup

In your GitLab project → Settings → Webhooks:

| Mode | Webhook URL |
|---|---|
| Local Multi-Process | `http://<your-host>:8000/webhook` (or `http://host.docker.internal:8000/webhook` if the GitLab container fires the hook from inside Docker Desktop) |
| Docker Compose (SSH) | `http://gateway:8000/webhook` (within `sdlcma_net`) |
| Docker Compose over HTTP | `http://gateway:8000/webhook` (within `sdlcma_net`) |
| gitlab.com via cloudflared | `https://<assigned>.trycloudflare.com/webhook` (quick tunnel; direct `http://<host>:8000/webhook` also works if inbound `:8000` is open) |
| AWS ECS | `https://<assigned>.trycloudflare.com/webhook` (cloudflared sidecar inside the ECS services task; URL changes every service task replacement) |
| Kubernetes / kind | `https://<assigned>.trycloudflare.com/webhook` (cloudflared Deployment; new URL on each pod restart — use a named tunnel for stability) |
| Kubernetes / kind (ingress) | `http://<host-or-tailnet-ip-or-hostname>:18080/webhook` (when GitLab can route to the agent host directly — ingress-nginx + kind `:18080→:80` port mapping; CN networks use the `m.daocloud.io` proxy that `infra/k8s/setup.sh` rewrites in) |

Trigger: **Pipeline events**

---

## Test Utilities

**Full testing runbook: [`tests/TESTING.md`](tests/TESTING.md)** — every
testing surface (unit, integration, evaluation sweeps, and the real-host
GitLab-mode end-to-end smokes) with exact run steps, expected results, the
GitLab-API cross-check, and coverage boundaries. The sections below are a
quick reference.

### One-command regression per deployment

Every deployment method — plus `integration_test.py` — has a single-entrypoint
regression script that **detects code/image staleness, updates if needed,
sets up the stack, runs an end-to-end smoke, then restores prior state**, all
with timestamped progress lines and standard exit codes (`0` PASS · `2` FAIL
· `3` TIMEOUT · `4` PRE-FLIGHT FAIL). This is the project's main "is this
still working?" entry point — re-running it is the fastest way to verify any
code change end-to-end against any deployment without thinking about which
preconditions you forgot.

| Script | Deployment | Smoke target |
|---|---|---|
| `tests/integration_test_wrapper.sh` | `integration_test.py` (in-process) | isolated Redis db=15 |
| `infra/local-gitlab/regression.sh` | Mode 1 — `local_multi_process` (systemd) | Windows docker-compose GitLab |
| `infra/local-docker-compose/regression.sh` | Mode 2 — `local_docker_compose` (containerized) | Windows docker-compose GitLab on `sdlcma_net` |
| `infra/public-host/regression.sh` | Mode 4 — `gitlab_saas` on public-IP host (cloudflared) | gitlab.com |
| `infra/aws-ecs/regression.sh` | Mode 5 — AWS ECS (`gitlab_saas` + `WORKER_SPAWNER=ecs`) | gitlab.com |
| `infra/k8s/regression.sh` | Mode 6 — Kubernetes / kind (`gitlab_saas` + `WORKER_SPAWNER=k8s`) | gitlab.com |

Common flags: `--timeout N`, `--no-update`, `--no-teardown`, `--keep-env`.
Full contract + per-script knobs in [`tests/TESTING.md`](tests/TESTING.md)
§4. Each `infra/*/` directory also retains the lower-level `setup.sh` /
`gitlab-smoke.sh` / `teardown.sh` building blocks for manual / partial runs.

### Integration Test

Runs the full pipeline (gateway → orchestrator → worker) against an isolated Redis DB with a synthetic bug report:

```bash
uv run python integration_test.py [--redis-url redis://...] [--bug-id BUG-IT-1] [--config configs/baseline_last_commit.json]
```

Passing `--config` sets `BF_AGENT_CONFIG` for the spawned worker. If the first
spec includes `agent_ref`, the integration test verifies the GitLab worker
handoff into that pinned checkout.

> Use `uv run python` rather than invoking a venv interpreter directly — `apply_change_and_test` shells out to `python -m venv` to set up an isolated test environment, and that requires `python` (not just `python3`) to be on PATH.

### Unit Tests

Targeted unit tests live under `tests/`. The patch-scope guardrail is the first thing covered there:

```bash
uv run pytest tests/
```

### Send Pipeline Message

Manually send a pipeline webhook payload to the gateway for testing:

```bash
python test_utility/send_pipeline_msg.py [--gateway-url http://localhost:8000] [--file path/to/msg.txt]
```

---

## Project Structure

```
├── gateway/                  # FastAPI webhook receiver
├── orchestrator/             # Async orchestrator (consumer, spawner, monitor, router)
├── bf_worker/
│   ├── agents/               # Agent abstraction layer (unit of comparison)
│   │   ├── base.py           #   Agent ABC, BugInput, FixOutput
│   │   ├── run_record.py     #   Canonical RunRecord schema
│   │   └── langgraph_agent.py  # default agent: wraps the LangGraph state machine + hooks
│   ├── enhancements/         # LangGraphAgent-only extension layer
│   │   ├── hooks.py          #   HookRegistry, HookName (named extension points)
│   │   ├── build_enhancements.py  # Spec-dispatch factory: {kind:...} → (hook, callback) tuples
│   │   ├── memory.py         #   Bundled memory-lookup enhancement (PRE_REACT_LOOP + AGENT_POST_FIX)
│   │   └── reflection.py     #   Bundled self-reflection enhancement (POST_APPLY_TEST)
│   ├── providers/
│   │   ├── base.py           # Provider ABCs (SourceProvider, VCSProvider, ReviewProvider)
│   │   ├── gitlab_provider.py  # GitLab implementation (owns the Repo helper for git CLI + GitLab REST)
│   │   └── local_provider.py   # Local git + no-git implementations
│   ├── graph/
│   │   ├── nodes/            # LangGraph nodes (platform-agnostic via provider)
│   │   ├── builder.py        # Graph definition and edges
│   │   ├── routing.py        # Conditional edge functions
│   │   └── state.py          # BugFixState TypedDict (includes provider ref)
│   ├── services/
│   │   ├── apply_patch.py    # Patch application (content-anchored; self-heals stale line_number, PatchAnchorError on mismatch)
│   │   ├── patch_guard.py    # Apply-time scope/sensitive-path/cap guardrail
│   │   ├── prompt_guard.py   # Prompt-injection defense for untrusted content
│   │   ├── fetch_guard.py    # Read-path containment for fetch_additional_file
│   │   ├── budget.py         # Per-run cap on LLM calls / tokens / wall-clock
│   │   ├── parse_trace.py    # Trace parsing (regex-based)
│   │   └── react_tools.py    # LLM tool definitions (provider-agnostic)
│   ├── journal.py            # Auto-captures running-mode runs for retrospective curation
│   ├── bf_worker.py          # Entry point for GitLab mode (with Redis heartbeat)
│   └── standalone.py         # Entry point for standalone local mode
├── evaluation/               # Evaluation mode: sweep agents × fixtures
│   ├── fixtures/             # Curated benchmark (10 single-file Python bugs by default)
│   ├── journal/              # Auto-captured runs from running mode (gitignored)
│   ├── runs/                 # Sweep outputs (gitignored)
│   ├── memory/               # Memory-enhancement store (pre-seeded JSON, append-mostly)
│   ├── fixture.py            # Fixture loader / discovery
│   ├── runner.py             # run_sweep(agent_specs, fixtures)
│   ├── metrics.py            # Aggregate run records into comparison tables
│   └── cli.py                # `bench` CLI: list / run / report / promote
├── configs/                  # Agent specs (consumed by evaluation sweeps and `standalone --config`)
│   ├── baseline.json         #   No-enhancements reference point
│   ├── memory.json           #   Memory-only single spec — pass to `bf_worker.standalone --config`
│   ├── memory_vs_baseline.json  # Baseline + memory enhancement, side by side (eval sweep)
│   └── reflection_vs_baseline.json  # Baseline + reflection enhancement, side by side (eval sweep)
├── settings/                 # Pydantic settings classes and .env files
├── test_utility/
│   ├── send_pipeline_msg.py  # Manual webhook sender
│   └── pipeline_msg.txt      # Sample pipeline payload
├── tests/                    # Unit tests (currently: patch_guard)
├── docker-compose.yml        # Docker Compose mode services
├── Dockerfile.gateway
├── Dockerfile.orchestrator
├── Dockerfile.bf-worker
└── integration_test.py       # End-to-end test
```

---

## Key Redis Data Structures

| Key / Stream | Purpose |
|---|---|
| `gateway:stream` | Webhook payloads from gateway to orchestrator |
| `worker:{bug_id}:stream` | Validation results routed to a specific worker |
| `orchestrator:dead_letter` | Failed messages with error details |
| `worker:heartbeat:{bug_id}` | TTL key; expiry signals a dead worker |

---

## License

MIT
