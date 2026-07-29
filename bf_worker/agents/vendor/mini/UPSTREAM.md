# Vendored mini-swe-agent — provenance and rules

## Pin

| | |
|---|---|
| Upstream | https://github.com/SWE-agent/mini-swe-agent |
| **Fork actually installed** | https://github.com/seek-far/mini-swe-agent |
| Checkout | `/mnt/d/my_git/mini-swe-agent` (editable install) |
| Version | `2.3.0` |
| Commit | `8a40ea289725436bb0a84aaf4393326cc631036d` |
| License | MIT — full text in `./LICENSE`, also reproduced in `/THIRD_PARTY_NOTICES.md` |

## What is vendored

| This file | Upstream path |
|---|---|
| `agent.py` | `src/minisweagent/agents/default.py` |
| `docker_env.py` | `src/minisweagent/environments/docker.py` |

**Nothing else.** Models (`minisweagent.models.*`), prompt templates, config
loading, exceptions and helpers are still imported from the installed upstream
package. The vendored files themselves still do `from minisweagent import ...`
— vendoring two classes must not become forking the library.

## Why these two, and only these two

SDLCMA needs a resumable SWE-bench run (plan item W2): when a worker is killed
mid-run, restart should re-attach to the still-running eval container and
continue from the last step instead of re-spending the whole trajectory.

That requires modifying exactly two things:

- the **loop** (`agent.py`) — to persist `messages` / `n_calls` / `cost` per
  step and to support entering the loop with a restored prefix;
- the **container lifecycle** (`docker_env.py`) — to attach to an existing
  container and to not destroy it on an abnormal exit.

Both are upstream implementation details with no extension seam, hence the copy.

## Rules

1. **DO NOT run a formatter over this directory.** As of the vendoring date the
   repository has no formatter configured; if one is ever introduced,
   `bf_worker/agents/vendor/` must be added to its exclude list.
2. **DO NOT edit these files without adding the change to the whitelist below.**
   `tests/test_vendor_mini_parity.py` compares them byte-for-byte against the
   installed upstream package after applying that whitelist. An unrecorded edit
   fails the test — that is the enforcement mechanism, not a nuisance.
3. When the upstream package is upgraded, the parity test **will fail**. That is
   intended: it forces a human to decide whether to re-sync. Re-sync procedure
   is at the bottom of this file.

## Allowed-difference whitelist

Everything the parity test normalizes away before comparing. Keep this list and
the test's normalization rules in sync — the test is the source of truth.

| # | File | Difference | Rationale | Added |
|---|---|---|---|---|
| 1 | both | The `# === SDLCMA VENDOR HEADER (begin) ===` … `(end) ===` block at the top | Provenance marker; stripped by the parity test | vendoring (W1) |
| 2 | `agent.py` | Four **added** lines: `self._sdlcma_resume()` after the seed messages in `run()`; `self._sdlcma_checkpoint()` in the loop's `finally`, next to `save()`; and the two methods themselves, defined here with empty bodies | The two missing seams for resumable runs. Everything else about W2 lives in `agents/mini_resume.py`, which subclasses this file — see below | W2 |

`docker_env.py` remains **byte-identical** below the header: `_start_container()`
and `cleanup()` are ordinary methods reached through `self`, so
`agents/mini_resume_env.py` overrides them from a subclass. It needs no
vendored change at all.

### Why entry 2 is only four lines

The resume logic itself is NOT here. `ResumableAgent` (in
`bf_worker/agents/mini_resume.py`) subclasses this file and overrides the two
hooks; this file only gains the two *call sites*, which is precisely what a
subclass cannot add because they sit inside `run()`'s body.

Consequences worth stating, because they are what keeps the parity story cheap:

- **No upstream line was removed or modified** — the diff is purely additive.
- With the hooks left as the no-ops defined here, this file behaves **exactly**
  like upstream, so W1's L1/L2/L3 equivalence results still hold for
  `MINI_IMPL=vendored` with `BF_STEP_CHECKPOINT` unset.
- `_sdlcma_checkpoint()` sits in `finally`, i.e. it also runs when `step()`
  raised. That is deliberate: `InterruptAgentFlow` (`Submitted`,
  `LimitsExceeded`, …) is caught by the `except` clause *before* `finally`, so
  the terminal `role="exit"` message is already in `self.messages` and gets
  recorded. Anything overriding these hooks MUST swallow its own exceptions —
  an exception raised inside `finally` would mask the agent's real one.

## Known behavioural differences from upstream (not code differences)

`DefaultAgent.serialize()` and `DockerEnvironment.serialize()` emit
`f"{self.__class__.__module__}.{self.__class__.__name__}"`. For the vendored
classes that resolves to `agents.vendor.mini.*` instead of `minisweagent.*`.

This is **intentional and harmless**:

- it makes a saved trajectory self-identifying about which implementation
  produced it, which is exactly what you want while both exist;
- these fields land only in the trajectory JSON. Nothing in SDLCMA reads
  `agent_type` / `environment_type` / `mini_version`, and they are not part of
  the LLM request body, so they cannot affect the gateway cache key, the cache
  hit rate, or `resolved`.

## ⚠️ Vendor from the INSTALLED package, not from any checkout you happen to find

SDLCMA drives an **editable-installed fork**, not upstream `main`. There may be
other mini-swe-agent checkouts on the same machine at different versions —
copying from one of those produces a vendored file that silently does not match
what actually runs. Always resolve the source with:

```bash
python -c "import minisweagent, os; print(os.path.dirname(minisweagent.__file__))"
```

(The parity test catches this, because it compares against
`inspect.getsourcefile()` of the imported module. It caught exactly this
mistake during the W1 vendoring.)

## Re-sync procedure

```bash
# 1. Resolve the INSTALLED package, note its commit
U=$(python -c "import minisweagent,os;print(os.path.dirname(minisweagent.__file__))")
cd "$U/../.." && git log -1 --format=%H && git describe --tags --always

# 2. Re-copy the two files
U="$U"   # from step 1
V=<repo>/bf_worker/agents/vendor/mini
# ... re-apply the header block, then copy the body verbatim ...

# 3. Re-apply every entry in the whitelist above (W2+ changes live there)
# 4. Update the Pin table in this file
# 5. uv run pytest tests/test_vendor_mini_parity.py
```
