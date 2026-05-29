# Cloud vs. Self-Hosted LLM — Initial Benchmark (2026-05-24)

First side-by-side comparison of two LLM backends on the bundled SDLCMA
fixture set, sweeping all 19 fixtures with the baseline agent (no enhancements).

**Headline numbers (one sweep each, see caveats):**

| Backend | fix_rate | avg tokens/cell | avg LLM s/cell | tokens/s | cost(USD) |
|---|---|---|---|---|---|
| **Cloud — Dashscope `qwen3-coder-480b-a35b-instruct`** | **0.947** (18/19) | 5,359 | 8.85 | 605 | 0.38 |
| **Self-hosted — vLLM (+FlashInfer) `qwen2.5-coder-32b-instruct-awq`** | 0.895 (17/19) | 6,179 | 9.70 | 637 | 0 |

Cloud has the higher fix rate and the lower per-cell token / wallclock cost.
Self-hosted has a slightly higher throughput once a call is in flight,
because there is no Dashscope-to-host network RTT to amortize. Neither
backend strictly dominates per-fixture — see the per-cell breakdown below.

> **Sample-size caveat.** These are **initial results**: one sweep per
> backend, 19 cells per sweep. The per-cell numbers are point estimates with
> no measured variance. Aggregate trends (cloud's higher fix_rate, the rough
> token / wallclock parity, the F03 / F19 multi-file pattern) are likely
> real; per-cell flips like F11 may be sampling noise. Re-running each sweep
> 3× would give defensible per-cell error bars.

---

## Setup

- **Fixtures**: the 19 bundled in `evaluation/fixtures/F01..F19/`, mix of
  easy single-file (off-by-one, type errors, missing edge cases, recursion
  base cases, …), medium api-misuse, and hard multi-file ("the test fails
  here but the bug is over there") cases.
- **Agent**: `configs/baseline.json` — `LangGraphAgent` with no enhancements
  (no memory, no reflection, no code-review). Same agent code for both
  sweeps; only the LLM backend changes.
- **Host**: Tailscale-reachable Linux box at `100.81.178.68`,
  driven over SSH from the WSL dev machine via `infra/remote-eval/deploy.sh`.
  Both sweeps ran in eval mode (no GitLab / orchestrator) via
  `python -m evaluation.cli run`.

### Cloud backend

- Provider: Alibaba Dashscope, OpenAI-compatible endpoint at
  `https://dashscope.aliyuncs.com/compatible-mode/v1`
- Model: `qwen3-coder-480b-a35b-instruct` (480B MoE, ~35B active per token,
  general-purpose coder)

### Self-hosted backend

- Server: **vLLM with FlashInfer attention backend**
- Endpoint: `http://127.0.0.1:8000/v1` on the same Tailscale host (no
  network hop between worker and backend)
- Model: `qwen2.5-coder-32b-instruct-awq` (32B dense, AWQ 4-bit
  quantization, code-specialized)
- Hardware: single GPU on the Tailscale host

The "self-hosted" discriminator in SDLCMA is `LLM_API_KEY=="EMPTY"`
(see `CLAUDE.md` Configuration section). The worker startup probe
(`services/llm_model_check.check_or_abort`) verifies the env-declared
`LLM_MODEL` matches what vLLM's `GET /v1/models` reports, aborting on
mismatch unless `LLM_ALLOW_MODEL_MISMATCH=1` is set. The probe passed
cleanly for this run — both names were `qwen2.5-coder-32b-instruct-awq`.

---

## Per-fixture results

`out` = outcome (`fixed` / `error`), `it` = `iterations` (extra fix retries
after the first attempt), `ptok` = `total_prompt_tokens` +
`total_completion_tokens`, `wll` = `total_llm_wallclock_s` (sum of
`time.perf_counter()` deltas around every LLM call this cell).
**`←`** marks cells where the two backends differ in outcome.

| fixture | cloud (out it ptok wll) | self-hosted (out it ptok wll) | notes |
|---|---|---|---|
| F01-off-by-one              | fixed 0  4,331  5.6 | fixed 0  4,548  8.1 | |
| F02-type-coercion           | fixed 0  2,042  4.9 | fixed 0  2,103  5.2 | |
| F03-missing-key             | fixed 0  2,135  6.0 | error 0 22,145 24.4 | **←** cloud wins; SH burnt 22k tokens before aborting |
| F04-case-insensitive        | fixed 0  4,206  6.4 | fixed 0  4,386  6.3 | |
| F05-empty-list              | fixed 0  2,097  7.9 | fixed 0  4,378 10.1 | SH used 2× the tokens |
| F06-mutable-default         | fixed 0  4,354  6.9 | fixed 0  4,558  7.2 | |
| F07-string-slicing          | fixed 0  4,201  6.2 | fixed 0  4,298  6.6 | |
| F08-recursion-base          | fixed 0  1,924  3.7 | fixed 0  2,173  5.8 | |
| F09-float-precision         | fixed 1 11,843 14.9 | fixed 0  4,685  6.7 | SH one-shotted; cloud needed a retry |
| F10-whitespace              | fixed 0  4,278  8.1 | fixed 0  4,275  5.7 | |
| F11-half-up                 | error 2  7,393  9.5 | fixed 0  4,508  8.3 | **←** SH wins; cloud retried twice and gave up |
| F12-ceil-pages              | fixed 0  4,294  8.5 | fixed 1  7,489 15.1 | cloud one-shotted; SH needed a retry |
| F13-clamp                   | fixed 0  4,288  6.9 | fixed 0  4,480  8.4 | |
| F14-trimmed-mean            | fixed 0  4,412  9.2 | fixed 0  4,541  6.8 | |
| F15-span                    | fixed 0  4,649 10.7 | fixed 0  4,643  7.9 | |
| F16-tree-total              | fixed 0  4,398  8.9 | fixed 0  4,445  7.5 | |
| F17-span-firstchar          | fixed 0  6,520 10.3 | fixed 0  6,532  8.5 | multi-file, both succeeded |
| F18-shadow-validator        | fixed 1 15,270 21.9 | fixed 0  4,467  9.3 | SH one-shotted multi-file; cloud needed a retry |
| F19-rate-table              | fixed 0  9,189 11.7 | error 2 18,749 26.5 | **←** cloud wins; SH burnt 18k tokens, MAX_FIX_RETRIES |

### Outcome flips (3 of 19 cells)

- **F03 missing-key, F19 rate-table** — cloud fixes, self-hosted errors.
  Both belong to the "broader search needed" pattern: missing-key reasoning
  and multi-file dependency resolution respectively. The 480B model
  succeeds first-attempt; the 32B-AWQ model exhausts retries and burns
  ~20k tokens per failed cell.
- **F11 half-up** — reverse: self-hosted one-shots; cloud retries twice and
  gives up. F11 sits in the `oscillation-trap` category (the model can
  produce a fix that flips the bug to the symmetric mirror image — round
  half-up → round half-down). Whether this is a stable advantage for
  Qwen2.5-Coder or a single-sample fluke is the most interesting question
  this benchmark cannot answer with one sweep.

---

## FlashInfer effect on the self-hosted backend

Today's self-hosted run is the first to use vLLM's **FlashInfer** attention
backend (installed earlier today on the Tailscale host). To estimate the
effect we compared today's run against the previous self-hosted runs from
2026-05-23, which used vLLM's default attention backend (same model, same
hardware, same fixtures).

The 2026-05-23 runs predate the latency telemetry added today
(`llm_call_count` / `total_*_tokens` / `total_llm_wallclock_s` /
`llm_model_served`), so we only have `elapsed_s` (total wall time per cell,
including venv setup and pytest) and `max_input_tokens` from those records
— not per-call LLM-only timing. Three full sweeps were available from
2026-05-23; we kept only cells where **both** today and yesterday's
best-of-3 succeeded, leaving **17 fixtures** for the apples-to-apples
comparison.

| | yesterday best-of-3 (no FlashInfer) | today (FlashInfer) |
|---|---|---|
| Sum elapsed_s (17 cells) | **375.7 s** | **345.9 s** |
| Delta | — | **−29.7 s (−7.9 %)** |

Per-fixture, today's elapsed_s is 4–10 % lower than yesterday's best, with
zero regressions. Subtracting the per-cell venv-setup-plus-pytest overhead
(estimated at ~10.7 s from today's `elapsed_s − total_llm_wallclock_s`
delta) suggests the **LLM-only portion improved by ≈ 15 %** (1.7 s on a
~11.4 s baseline). This is a rough back-of-envelope estimate, not a
measured value — see the caveat below.

> **FlashInfer caveats.** Yesterday's runs lacked per-LLM-call timing, so
> the 15 % "LLM-only" number is inferred from total wallclock with a
> uniform overhead assumption, not directly measured. The 17-cell sample is
> small, and the 3 yesterday-runs themselves had wide variance (one of
> three sweeps failed F01 — likely a backend hiccup, not a fixture
> property). The cleanest validation would be a full 3-vs-3 sweep
> comparison once we have a few more days of self-hosted runs with the new
> telemetry.

---

## Discussion

### Where cloud (Qwen3-Coder-480B) wins

- **Hard multi-file / broader search** (F03, F19): the 480B model gets the
  fix on the first try with 2–9k tokens; the 32B model exhausts retries
  burning 18–22k tokens per failed cell. This is the single biggest cost
  driver in the aggregate token comparison (cloud's 13 % token savings come
  mostly from not burning these failed-cell tokens).
- **Aggregate fix_rate**: +5.2 percentage points (94.7 % vs. 89.5 %).

### Where self-hosted (Qwen2.5-Coder-32B-AWQ) wins

- **Simple algorithmic bugs done one-shot** (F09 float-precision, F18
  shadow-validator multi-file, possibly F11 half-up): the
  code-specialized 32B model converged on the right fix without a retry,
  while the larger generalist needed an extra round.
- **No network round-trip per call**: per-call wallclock is in the same
  ballpark even though the model is 15× smaller (480B vs. 32B). The
  reason is that local vLLM eliminates the Dashscope RTT, which roughly
  offsets the model-size difference at these prompt lengths.
- **Cost**: self-hosted token cost is GPU time you already paid for; cloud
  cost is per-token billing.

### Aggregate `tokens/s` interpretation

The 605 vs. 637 tokens/s split (cloud lower) does **not** mean Dashscope
generates more slowly per call. It is the sum-of-tokens divided by
sum-of-wallclock across the whole sweep; the network RTT for every cloud
call inflates the denominator. Per-call generation rate of the cloud
backend is almost certainly higher; this metric just measures end-to-end
throughput including network, which is what `bench report` users care
about when comparing backends.

---

## Caveats summary

1. **One sweep per backend.** No measured per-cell variance. The 3
   outcome flips (F03, F11, F19) may include sampling noise, especially
   F11.
2. **The FlashInfer comparison uses elapsed_s only**, not per-LLM-call
   timing — yesterday's records predate the new telemetry. The "~15 %
   LLM-only improvement" is inferred, not measured.
3. **Different model sizes**: Qwen3-Coder-480B (cloud) vs.
   Qwen2.5-Coder-32B-AWQ (self-hosted). The model is the dominant
   variable; the backend / network is secondary. This is "cloud vs.
   self-hosted at the configurations a real user would pick", not "same
   model, two backends".
4. **Cloud network conditions** were not controlled. RTT to Dashscope can
   vary by tens of milliseconds.
5. **Both backends saw `total_cached_input_tokens=None` for every cell** —
   neither surfaces prompt-cache hits in the OpenAI-compatible usage block
   we read. Cache-hit telemetry will only appear on backends that report
   it (OpenAI direct, newer vLLM builds, …).

---

## Methodology

Reproduce locally (against a vLLM serving the named model on the remote):

```bash
# Self-hosted sweep
bash infra/remote-eval/deploy.sh   # rsync + discover served name + rewrite env
ssh ls@<host> "cd ~/sdlcma && source .venv-linux/bin/activate && \
  uv run python -m evaluation.cli run \
    --config configs/baseline.json \
    --run-id baseline_full_2026_05_24"

# Cloud sweep (after temporarily swapping the worker env to point at Dashscope)
ssh ls@<host> "cd ~/sdlcma && source .venv-linux/bin/activate && \
  uv run python -m evaluation.cli run \
    --config configs/baseline.json \
    --run-id baseline_full_2026_05_24_cloud"

# Pull results and report
rsync -az ls@<host>:~/sdlcma/evaluation/runs/baseline_full_2026_05_24/ \
  evaluation/runs/baseline_full_2026_05_24/
uv run python -m evaluation.cli report baseline_full_2026_05_24
uv run python -m evaluation.cli report baseline_full_2026_05_24_cloud
```

The aggregate columns (`avg_total_tokens`, `avg_llm_wallclock_s`,
`tokens_per_s`) are produced by `evaluation/metrics.py:aggregate`. The
per-cell numbers in the per-fixture table above come from
`evaluation/runs/<run_id>/summary.json` (the per-cell `RunRecord`s; see
`bf_worker/agents/run_record.py` for the schema).
