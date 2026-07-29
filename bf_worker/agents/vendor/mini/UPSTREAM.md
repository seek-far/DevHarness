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

| # | Difference | Rationale | Added |
|---|---|---|---|
| 1 | The `# === SDLCMA VENDOR HEADER (begin) ===` … `(end) ===` block at the top of each file | Provenance marker; stripped by the parity test | vendoring (W1) |

Below the header, the files are **byte-identical** to upstream. No import
rewriting was needed.

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
