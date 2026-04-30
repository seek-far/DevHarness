# DevHarness

**DevHarness** is an automated bug-fixing agent powered by an LLM ReAct loop. It diagnoses test/CI failures, generates patches, validates them locally, and delivers the fix — either as a GitLab merge request or a local patch file.

It is also a research platform: the bug-fix approach itself is pluggable (`Agent` interface), and an evaluation harness compares approaches against a curated benchmark.

---

## Two Modes of Operation

### GitLab Mode (Full Pipeline)

Listens for GitLab CI failure webhooks, diagnoses and fixes the bug automatically, then opens a merge request — no human intervention needed.

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

### GitLab Mode

```bash
# 1. Gateway (webhook receiver)
uvicorn gateway.gateway:app --host 0.0.0.0 --port 8000

# 2. Orchestrator
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

Currently wired hook points: `agent.pre_fix`, `agent.post_fix` (called from `LangGraphAgent.fix()`), and `graph.pre_react_loop` (called from `graph/nodes/react_loop.py` — used by the memory enhancement to inject a `memory_hint` into the initial prompt). Other graph-internal points (`graph.post_react_loop`, `graph.pre_apply_test`, `graph.post_apply_test`) are *named* but their call sites in the graph nodes are added when the first enhancement that needs them lands — adding hook calls without a concrete consumer would be premature.

#### Bundled enhancement: memory lookup

`bf_worker/enhancements/memory.py` is a token-overlap memory of past fixes. It registers a `PRE_REACT_LOOP` callback (queries `evaluation/memory/store.json` using `error_info` + `suspect_file_path` and injects up to `top_k` matches as `state["memory_hint"]`, which the ReAct prompt appends as a "Prior similar fixes (reference only)" section) and an `AGENT_POST_FIX` callback (appends each run's outcome to the store). The store is pre-seeded with 10 category-keyed lessons so the first sweep has something to retrieve. Compare baseline vs memory with `configs/memory_vs_baseline.json`.

### RunRecord (canonical telemetry schema)

`bf_worker/agents/run_record.py` defines the `RunRecord` dataclass — the single source of truth for the structured outcome of one `agent.fix()` invocation. Both the running-mode journal and the evaluation runner write the same shape, so downstream tooling (metrics, promotion, dashboards) only handles one schema. Bump `SCHEMA_VERSION` for incompatible changes.

### Journal & Evaluation

Every running-mode invocation writes a `RunRecord` to `evaluation/journal/<ts>_<bug_id>_<agent>/`. Auto-flagged candidates (failures, no-fix, high-iteration runs) can later be promoted into curated **fixtures** for the benchmark via `python -m evaluation.cli promote`.

```bash
python -m evaluation.cli list-fixtures                               # what's in the benchmark
python -m evaluation.cli list-journal --flagged                      # candidates worth promoting
python -m evaluation.cli promote <journal_entry> --category off-by-one
python -m evaluation.cli run --config configs/baseline.json          # sweep configured agents × fixtures
python -m evaluation.cli run --fixture-id F01-off-by-one F03-missing-key  # subset
python -m evaluation.cli report <run_id>                             # comparison table
```

The journal is always-on (override path with `BF_JOURNAL_DIR`); evaluation runs are sandboxed and never modify your real source.

#### Bundled fixtures

10 single-file Python bugs in `evaluation/fixtures/` covering off-by-one, type-coercion, missing edge cases, recursion base case, mutable defaults, float precision, and string handling. Each fixture is self-contained (`source/` + `meta.json` + `requirements.txt`). See `evaluation/fixtures/F01-off-by-one/` for the canonical layout.

#### Configs

Agent specs live as JSON lists under `configs/`. The `baseline.json` config (no enhancements) is the reference point against which future enhancements are measured. To compare approaches, write a config listing both, run, and compare — `configs/memory_vs_baseline.json` is a worked example:

```bash
python -m evaluation.cli run --config configs/baseline.json            # baseline only
python -m evaluation.cli run --config configs/memory_vs_baseline.json  # baseline + memory side by side
python -m evaluation.cli report run_<timestamp>
```

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
fetch_trace → parse_trace → fetch_source_file → react_loop
    → create_fix_branch → apply_change_and_test → commit_change
    → wait_ci_result → create_mr → END
                                          ↓ (any failure)
                                     handle_failure
```

The **ReAct loop** gives the LLM tools (`fetch_additional_file`, `fetch_file_segment`, `submit_fix`, `abort_fix`) and runs up to 8 reasoning steps. The patch is applied and tested in an isolated Python venv before being committed.

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
| `LLM_API_KEY` | API key for your LLM provider |
| `LLM_API_BASE_URL` | OpenAI-compatible base URL (e.g. Dashscope) |
| `LLM_MODEL` | Model name (e.g. `qwen3-coder-480b-a35b-instruct`) |

---

## Running (GitLab Mode Details)

DevHarness supports two GitLab deployment modes, controlled by `settings/.env`:

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

### GitLab Webhook Setup

In your GitLab project → Settings → Webhooks:

| Mode | Webhook URL |
|---|---|
| Local Multi-Process | `http://<your-host>:8000/webhook` |
| Docker Compose | `http://gateway:8000/webhook` (within `sdlcma_net`) |

Trigger: **Pipeline events**

---

## Test Utilities

### Integration Test

Runs the full pipeline (gateway → orchestrator → worker) against an isolated Redis DB with a synthetic bug report:

```bash
uv run python integration_test.py [--redis-url redis://...] [--bug-id BUG-IT-1]
```

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
│   │   └── hooks.py          #   HookRegistry, HookName (named extension points)
│   ├── providers/
│   │   ├── base.py           # Provider ABCs (SourceProvider, VCSProvider, ReviewProvider)
│   │   ├── gitlab_provider.py  # GitLab implementation
│   │   └── local_provider.py   # Local git + no-git implementations
│   ├── graph/
│   │   ├── nodes/            # LangGraph nodes (platform-agnostic via provider)
│   │   ├── builder.py        # Graph definition and edges
│   │   ├── routing.py        # Conditional edge functions
│   │   └── state.py          # BugFixState TypedDict (includes provider ref)
│   ├── services/
│   │   ├── gitlab_utils.py   # GitLab API and git operations (used by GitLabProvider)
│   │   ├── apply_patch.py    # Patch application logic
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
├── configs/                  # Agent specs for evaluation sweeps
│   ├── baseline.json         #   No-enhancements reference point
│   └── memory_vs_baseline.json  # Baseline + memory enhancement, side by side
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
