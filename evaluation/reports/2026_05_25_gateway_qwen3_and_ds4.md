# LLM Gateway — Two-Cloud Ladder (Qwen3 + DeepSeek-v4-pro) (2026-05-25)

Run id: `run_20260525T181559Z`. First sweep on the new two-cloud gateway
configuration `qwen3_and_ds4.yaml`, paired with the new
**no-fix retry** edge (Plan A) that lets `route_after_react_loop`
self-loop back into `react_loop` when the gateway is in the path and the
prior backend exited without a `submit_fix`.

**Headline:**

| Sweep | fix_rate | cells via qwen3 | cells via ds4 | avg toks/cell | avg LLM s/cell | cost(USD) |
|---|---|---|---|---|---|---|
| **This run — gateway, [qwen3_dashscope, deepseek_v4_pro]** | **1.000 (19/19)** | 18 (attempt=0) | 1 (F10, after 1 fix_retry) | 4,610 | 8.67 | 0.36 |
| Cloud only — Dashscope qwen3 (2026-05-24) | 0.947 (18/19) | 19 | — | 5,359 | 8.85 | 0.38 |
| Self-hosted only — vLLM Qwen2.5-Coder-32B (2026-05-24) | 0.895 (17/19) | — | — | 6,179 | 9.70 | 0 |

**19/19** — the first sweep on the bundled fixture set that does not
leave a cell un-fixed. The single cell where the fallback engaged
(F10-whitespace) was rescued by `deepseek_v4_pro` after qwen3's
attempt=0 produced a patch that apply+test rejected.

> **Sample-size caveat.** One sweep, 19 cells, no per-cell variance.
> The cloud-only baseline lost F11 in its 2026-05-24 sweep; this run
> got F11 first-try with qwen3 — could be sampling noise on either
> side. The 19/19 headline is one observation, not a guarantee.

---

## Setup

- **Fixtures**: the 19 bundled in `evaluation/fixtures/F01..F19/`.
- **Agent**: `configs/baseline.json` (LangGraphAgent, no enhancements).
- **Host**: WSL dev box at `/mnt/d/my_git/sdlcma/v08`. Eval ran in-process
  (no GitLab / orchestrator / Redis); the gateway ran as a separate
  uvicorn process on `127.0.0.1:9000`.
- **Worker → gateway**: `LLM_API_BASE_URL=http://127.0.0.1:9000/v1` +
  `LLM_VIA_GATEWAY=true`. Worker attaches `X-Sdlcma-Bug-Id` and
  `X-Sdlcma-Attempt` on every LLM call.

### Inference policy

`type: ordered`, `advance_on_fix_failure: true`. Order on disk = the
order in the yaml file:

- attempt 0 → `qwen3_dashscope`
- attempt ≥ 1 → `deepseek_v4_pro`

The gateway's policy is stateless — selection is a pure function of
attempt. The worker sums `fix_retry_count + no_fix_retry_count` and
sends the total in `X-Sdlcma-Attempt`.

### Backends

| Name | Provider | Model | Notes |
|---|---|---|---|
| `qwen3_dashscope` | Alibaba Dashscope | `qwen3-coder-480b-a35b-instruct` | 480B MoE, ~35B active, coder generalist |
| `deepseek_v4_pro` | DeepSeek | `deepseek-v4-pro` | reasoning model — emits answer in `reasoning_content` AND uses structured tool_calls when bound; verified mid-run by the F10 rescue |

### Plan A: no-fix retry (new this run)

When `react_loop` exits with `llm_result=None` (`MAX_STEPS` reached or
`abort_fix` called), the worker now bumps a separate
`no_fix_retry_count`, and `route_after_react_loop` self-loops back to
`react_loop` if (a) `cfg.llm_via_gateway=true` and (b) the counter is
within `NO_FIX_MAX_RETRIES=1`. The combined gateway attempt header is
`fix_retry_count + no_fix_retry_count`, so the policy escalates
uniformly on either failure mode.

**Plan A did not fire in this sweep** — `no_fix_retries_fired = 0`.
Every cell either produced a `submit_fix` at attempt=0 (18 cells) or
went through the existing apply+test retry path (1 cell, F10). The
new edge is in place but unexercised; it remains the rescue path for
the F03-style "local exhausts MAX_STEPS without a proposal" scenario
that this configuration does not trigger.

---

## Per-fixture results

`out`/`it`/`tok`/`wll` as before; `nfr` = `no_fix_retry_count`;
`backend` = `llm_backend_name` (the gateway's last echoed backend).

| fixture | out | it | nfr | tok | wll s | backend | notes |
|---|---|---|---|---|---|---|---|
| F01-off-by-one        | fixed | 0 | 0 |  4,458 |  5.3 | qwen3_dashscope | |
| F02-type-coercion     | fixed | 0 | 0 |  2,080 |  3.7 | qwen3_dashscope | |
| F03-missing-key       | fixed | 0 | 0 |  2,137 |  4.2 | qwen3_dashscope | (the 5/24 self-hosted error case) |
| F04-case-insensitive  | fixed | 0 | 0 |  4,249 |  5.7 | qwen3_dashscope | |
| F05-empty-list        | fixed | 0 | 0 |  2,057 |  3.7 | qwen3_dashscope | |
| F06-mutable-default   | fixed | 0 | 0 |  4,310 |  6.3 | qwen3_dashscope | |
| F07-string-slicing    | fixed | 0 | 0 |  4,143 |  4.9 | qwen3_dashscope | |
| F08-recursion-base    | fixed | 0 | 0 |  1,970 |  2.6 | qwen3_dashscope | |
| F09-float-precision   | fixed | 0 | 0 |  4,581 |  5.0 | qwen3_dashscope | |
| **F10-whitespace**    | fixed | 1 | 0 |  9,048 | 42.6 | **deepseek_v4_pro** | **qwen3 attempt=0 produced a rejected patch → ladder → ds4 fixed it on retry** |
| F11-half-up           | fixed | 0 | 0 |  4,330 |  5.7 | qwen3_dashscope | (the 5/24 cloud error case — first-try here) |
| F12-ceil-pages        | fixed | 0 | 0 |  4,467 |  7.2 | qwen3_dashscope | |
| F13-clamp             | fixed | 0 | 0 |  3,967 |  3.5 | qwen3_dashscope | |
| F14-trimmed-mean      | fixed | 0 | 0 |  4,438 |  5.6 | qwen3_dashscope | |
| F15-span              | fixed | 0 | 0 |  4,598 |  6.5 | qwen3_dashscope | |
| F16-tree-total        | fixed | 0 | 0 |  4,323 |  7.9 | qwen3_dashscope | |
| F17-span-firstchar    | fixed | 0 | 0 |  6,632 |  7.9 | qwen3_dashscope | multi-file, one-shot |
| F18-shadow-validator  | fixed | 0 | 0 |  6,748 | 12.1 | qwen3_dashscope | |
| F19-rate-table        | fixed | 0 | 0 |  9,061 | 24.4 | qwen3_dashscope | (the 5/24 self-hosted error case) |

---

## Aggregates

| | this run | cloud only (5/24) | self-hosted only (5/24) |
|---|---|---|---|
| fix_rate | **1.000** | 0.947 | 0.895 |
| total LLM calls | 39 | — | — |
| total tokens (prompt+completion) | 87,597 | 95,820 | 117,453 |
| total LLM wallclock s | 164.8 | 168.3 | 186.4 |
| avg toks / cell | 4,610 | 5,359 | 6,179 |
| avg LLM s / cell | 8.67 | 8.85 | 9.70 |
| tokens / s | 532 | 605 | 637 |
| cells via qwen3_dashscope | 18 | 19 | 0 |
| cells via deepseek_v4_pro | 1 | 0 | 0 |
| `no_fix_retries_fired` | 0 | — | — |
| `apply_test_retries_fired` | 1 (F10) | 3 (F09, F11, F18) | 2 (F12, F19) |

This run is **the lowest per-cell token average and the lowest per-cell
wallclock average** of the three sweeps, while also being the only
sweep at 100 % fix_rate. The token saving vs cloud-only (~ 8 k tokens
across 19 cells) is mostly the F10 rescue — without the ladder, qwen3
would have either exhausted retries or kept producing rejected
patches.

The `tokens/s` regression vs the two baselines is a denominator
artefact, not a generation-speed regression: F10's 42.6 s on ds4 is
dominated by ds4's reasoning_content production (long internal CoT
before the tool call), which inflates wallclock without proportional
output tokens.

---

## Discussion

### Where the ladder helped (F10)

F10-whitespace was the only cell where the policy escalated. qwen3 at
attempt=0 took 1 react_loop step and produced a `submit_fix` — but the
patch failed apply+test (`fix_retry_count` advanced to 1). On the next
react_loop entry, the gateway saw `X-Sdlcma-Attempt: 1` and routed to
`deepseek_v4_pro`, which produced a patch that passed. Both single-
backend baselines (cloud-only and self-hosted-only) **also** fixed F10
at attempt=0 in their 2026-05-24 sweeps, so this is not a case where
the ladder unlocked a previously-unfixable cell — it's a case where one
specific qwen3 sample happened to flake on a cell qwen3 normally
handles, and the ladder caught it.

This is the **correct rescue shape** for the apply+test retry path:
qwen3 produced *something* (a proposal that turned out wrong), and
escalating to ds4 with the prior patch + truncated test_output as retry
feedback gave ds4 a sharper prompt than F10's initial framing.

### Why Plan A didn't fire

Plan A specifically targets the "react_loop exhausts MAX_STEPS or aborts
without a `submit_fix`" failure mode — typified by F03 on local in
prior runs (22 k tokens, never produced a patch). qwen3 at attempt=0
handled F03 in **1 step / 2 k tokens** here, so the dead-end never
materialized. Plan A's coverage is real but conditional on the primary
backend's behaviour; this configuration sidesteps the conditions Plan A
exists to handle.

If we re-ran with local as primary and ds4 as fallback (a
`local_plus_ds4.yaml` we don't have yet), F03 would plausibly engage
Plan A — local exhausts → no_fix retry → ds4 gets a fresh shot. Worth
testing.

### Cost: this run vs cloud-only

This run used 87,597 tokens. Cloud-only used 95,820. Difference =
~8,200 tokens — roughly the F10 retry delta (F10 alone consumed 9,048
tokens here vs 4,278 in the cloud-only sweep where it one-shotted; net
~5 k extra on F10 offset by similar-or-better single-shot performance
on other cells).

ds4's pricing isn't tracked in the run record. F10 consumed 9,048
tokens against ds4 — small in absolute terms, but ds4's billing rate
(if non-trivial) would dominate the comparison. The eval doesn't
currently surface per-backend token totals; that would be a useful
addition for cost-aware comparisons.

### DeepSeek-v4-pro is a reasoning model

The smoke test before the eval revealed ds4 emits a `reasoning_content`
field alongside `content`. langchain_openai's `ChatOpenAI.invoke` reads
`content` and `tool_calls` per the OpenAI spec — and ds4 populated
`tool_calls` properly for F10 (the rescue would have failed otherwise),
so the worker's read path works. But if a future cell calls ds4 at
react_loop's first step with no tool_calls in the response (only a
reasoning_content blob and an empty content), our existing
`_maybe_recover_tool_call_from_content` fallback would not find a JSON
in `content` to parse, and the cell would consume the MAX_STEPS budget
silently. Adding `reasoning_content` to the fallback's scan list is a
defensive change worth considering.

---

## Findings

1. **First 19/19 sweep on the bundled fixtures.** The two-cloud ladder
   recovered every cell — better than either single-backend baseline
   (qwen3-only: 18/19; vLLM-only: 17/19).
2. **The ladder paid off on F10**: ds4 fixed a qwen3 retry-path
   failure. One real rescue out of 19 cells.
3. **Plan A's new no-fix retry edge did not fire this sweep.** The
   conditions it's designed to rescue (react_loop exhausts MAX_STEPS
   without a submit_fix) didn't occur — qwen3 always produced a
   proposal at attempt=0. The edge is in place and unit-tested but
   needs a sweep with a weaker primary backend to exercise it for real.
4. **Average token + wallclock per cell are both lower** than both
   single-backend baselines, despite the F10 retry tail. The primary
   driver is more one-shot cells (none of the cloud-only single-cell
   retries — F09, F18 — recurred).
5. **DeepSeek-v4-pro's `reasoning_content` field is not currently
   consumed by the worker's tool-call recovery fallback.** Not a bug
   today (F10 went through cleanly) but a latent gap if a future cell
   gets a ds4 response with no `tool_calls` and the answer lives only
   in `reasoning_content`.

---

## Caveats summary

1. **One sweep, n=19.** Per-cell results are point estimates. F11
   flipped from error (cloud-only 5/24) to fixed (here) — could be
   sampling noise on either side. 19/19 might or might not hold under
   re-run.
2. **Two backends, not three.** A `qwen3 + ds4 + local` ladder would
   give Plan A a chance to fire on the F03-style failure mode at the
   local tier.
3. **Plan A unexercised end-to-end.** Unit tests cover the routing +
   bump semantics, but a real react_loop-exhaust → policy-advance →
   different-backend success has not been demonstrated by a fixture
   yet. The first config that would generate it is one where attempt=0
   uses a backend that exhausts MAX_STEPS on at least one cell — i.e.
   local as primary.
4. **Pricing not tracked.** Per-backend cost would need a separate
   accounting layer (or per-call backend attribution on `RunRecord`,
   which is currently last-call-only).
5. **DeepSeek model name (`deepseek-v4-pro`)** is what the user
   provided; this report doesn't take a stance on whether that maps to
   a standard DeepSeek public offering or a custom deployment.

---

## Data

- This run: `evaluation/runs/run_20260525T181559Z/`
- Side-by-side baselines:
  - `evaluation/runs/baseline_full_2026_05_24_cloud/` (cloud only)
  - `evaluation/runs/baseline_full_2026_05_24/` (self-hosted only)
- Prior report: `evaluation/reports/2026_05_24_cloud_vs_self_hosted.md`
- Gateway config: `configs/llm_gateway/qwen3_and_ds4.yaml`
- Plan A implementation: `bf_worker/graph/routing.py`,
  `bf_worker/graph/nodes/react_loop.py`,
  `tests/test_no_fix_retry.py`. Architecture contract in
  `docs/architecture.md` under "ReAct loop → No-fix retry".
