# DevHarness

**DevHarness** is an automated bug-fixing agent for CI/CD pipelines. It listens for GitLab CI failure webhooks, uses an LLM-powered ReAct loop to diagnose the failure and generate a patch, validates the fix locally, then opens a merge request — all without human intervention.

---

## How It Works

```
GitLab CI fails
      │
      ▼
[Gateway]  ── webhook received ──►  gateway:stream (Redis)
      │
      ▼
[Orchestrator]  ── spawns ──►  [Worker] (one per bug)
                                    │
                          ┌─────────▼──────────┐
                          │   LangGraph nodes   │
                          │  fetch_trace        │
                          │  parse_trace        │
                          │  fetch_source_file  │
                          │  react_loop (LLM)   │
                          │  create_fix_branch  │
                          │  apply + pytest     │
                          │  commit + push      │
                          │  wait CI result     │
                          │  create_mr          │
                          └────────────────────┘
```

The **Worker** runs a LangGraph state machine. The LLM is given the CI trace and source file; it calls tools (`fetch_additional_file`, `fetch_file_segment`, `submit_fix`, `abort_fix`) until it produces a patch or gives up. The patch is applied and tested in an isolated Python venv before being committed.

---

## Architecture

Three independently-running services communicate via **Redis Streams**:

| Service | Role |
|---|---|
| **Gateway** | Stateless FastAPI app. Receives GitLab webhooks and writes them to `gateway:stream`. |
| **Orchestrator** | Async event loop. Reads the stream, spawns one Worker subprocess per bug, monitors heartbeats, routes validation results back to workers. |
| **Worker** | Spawned once per bug. Runs the LangGraph fix pipeline, maintains a Redis heartbeat, cleans up on exit. |

---

## Requirements

- Python 3.10+
- Redis 7+
- GitLab instance (self-hosted or cloud) with webhook support
- An OpenAI-compatible LLM API (tested with Alibaba Dashscope / Qwen)

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
| `GITLAB_PRIVATE_TOKEN` | GitLab personal access token with `api` scope |
| `LLM_API_KEY` | API key for your LLM provider |
| `LLM_API_BASE_URL` | OpenAI-compatible base URL (e.g. Dashscope) |
| `LLM_MODEL` | Model name (e.g. `qwen3-coder-480b-a35b-instruct`) |

---

## Running

DevHarness supports two run modes, controlled by `settings/.env`:

### Mode 1: Local Multi-Process (`ENV=local_multi_process`)

Services run as separate processes on the host. Workers are spawned as subprocesses by the orchestrator.

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

### Mode 2: Docker Compose (`ENV=local_docker_compose`)

Gateway, Orchestrator, and Redis run as Docker containers. Workers are spawned as separate containers on demand by the orchestrator via the Docker API.

**Prerequisites:**
- An external Docker network `sdlcma_net` shared with the GitLab compose stack
- SSH private key configured in `settings/orchestrator_local_docker_compose.env`

```bash
# Create the shared network (if not already created)
docker network create sdlcma_net

# Build service images
docker compose build

# Build the worker image (not part of compose services)
docker build -f Dockerfile.bf-worker -t dh-bf-worker:latest .

# Start services
docker compose up
```

**Configuration files for Docker Compose mode:**

```bash
cp settings/orchestrator_local_docker_compose.env.example  settings/orchestrator_local_docker_compose.env
cp settings/worker_local_docker_compose.env.example        settings/worker_local_docker_compose.env
cp gateway/gateway_local_docker_compose.env.example        gateway/gateway_local_docker_compose.env
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
python integration_test.py [--redis-url redis://...] [--bug-id BUG-IT-1]
```

> There are no unit tests — only this end-to-end integration test.

### Send Pipeline Message

Manually send a pipeline webhook payload to the gateway for testing:

```bash
python test_utility/send_pipeline_msg.py [--gateway-url http://localhost:8000] [--file path/to/msg.txt]
```

Reads from `test_utility/pipeline_msg.txt` by default.

---

## Project Structure

```
├── gateway/                  # FastAPI webhook receiver
├── orchestrator/             # Async orchestrator (consumer, spawner, monitor, router)
├── bf_worker/
│   ├── graph/
│   │   ├── nodes/            # LangGraph nodes (one file per step)
│   │   ├── builder.py        # Graph definition and edges
│   │   ├── routing.py        # Conditional edge functions
│   │   └── state.py          # BugFixState TypedDict
│   ├── services/
│   │   ├── gitlab_utils.py   # GitLab API and git operations
│   │   ├── apply_patch.py    # Patch application logic
│   │   └── react_tools.py    # LLM tool definitions
│   └── entrypoint.sh         # Docker entrypoint (SSH key setup)
├── settings/                 # Pydantic settings classes and .env files
├── test_utility/
│   ├── send_pipeline_msg.py  # Manual webhook sender
│   └── pipeline_msg.txt      # Sample pipeline payload
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
