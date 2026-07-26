"""Cache key derivation for the LLM gateway.

The key MUST be derived from data the worker controls deterministically
(user + tool messages, system prompt, tools, params) and MUST NOT depend
on data the LLM produces (assistant messages with their stochastic
tool_call IDs and wording). Otherwise the second turn of a ReAct loop
re-hashes the LLM's own previous output and the cache silently misses
across replays.

Specifically:
  * `assistant` messages are EXCLUDED from the key entirely.
  * `tool` messages keep their content but DROP `tool_call_id` (the
    SDK-generated correlation ID — random per call, irrelevant to the
    semantic content).
  * `system` / `user` / `tool` content is canonicalised (sort keys on any
    nested JSON-like dict, normalise whitespace not in string values).
  * `tools` (the function declarations) participate — if the worker
    upgrades the `submit_fix` schema, old cache must miss.
  * `params` participates — temperature change invalidates cache.

The hash is SHA-256 hex; we surface a short prefix (first 12 chars) in
logs to make cache events greppable without leaking the full key.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

# Parameter fields that influence the model's output and MUST be part of
# the key. `seed` is honored for true reproducibility on backends that
# support it; `stream` is excluded because it's a transport detail.
_PARAM_FIELDS = (
    "temperature", "top_p", "top_k", "max_tokens", "max_completion_tokens",
    "presence_penalty", "frequency_penalty", "seed", "stop", "response_format",
)

# Keys we strip from individual tool messages before hashing. tool_call_id
# is a per-request random correlation ID (e.g. "call_HZ7q...") emitted by
# the assistant in the prior turn; the cache key must be invariant to it.
_TOOL_MSG_DROP = ("tool_call_id",)


def _normalize_messages(messages: Any, normalize_content: bool = False) -> list[dict[str, Any]]:
    """Strip messages down to the deterministic, external-only subset.

    Returns a list of dicts with at most {role, content, name} (and
    nothing else). Tool-result messages keep `name` if present; everything
    else is dropped. assistant messages are excluded.

    When `normalize_content` is True, every kept string content is run
    through `_normalize_volatile_content` to strip per-run timestamps,
    runner IDs, Docker digests etc. Off by default — turning it on is
    an opt-in for stress-test cacheability where the trace machinery
    metadata differs between identical fixtures.
    """
    if not isinstance(messages, list):
        return []
    out: list[dict[str, Any]] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role not in ("system", "user", "tool"):
            # Drop assistant + any unknown role. The whole point is to key on
            # what the worker sent, not what prior turns of the LLM said.
            continue
        content = m.get("content")
        if normalize_content and isinstance(content, str):
            content = _normalize_volatile_content(content)
        kept: dict[str, Any] = {"role": role, "content": content}
        # For tool messages, retain `name` (the function name) but drop
        # tool_call_id. `name` is part of the semantic input (which function
        # this is a result for); tool_call_id is correlation metadata.
        for k in ("name",):
            if k in m:
                kept[k] = m[k]
        for k in _TOOL_MSG_DROP:
            kept.pop(k, None)
        out.append(kept)
    return out


# Patterns matching content that's deterministic per fixture-attempt but
# varies between fixture runs. Each pattern's replacement is a stable
# placeholder so the surrounding semantic structure of the trace is kept
# (a reader can still tell where a timestamp WOULD have been).
#
# Conservative principle: only match shapes that are unambiguously
# machinery metadata, never plausible source-code or test-error content.
# Adding a too-broad pattern that strips real bug content is a worse
# failure mode than not normalizing — the latter only lowers hit rate;
# the former silently feeds the LLM the wrong cached response.
_NORMALIZE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # ANSI SGR colour escapes — MUST be first. pytest colours its summary
    # (`\x1b[32m201 passed\x1b[0m, … in 0.84s\x1b[0m`) when the eval container's
    # stdout is a tty; the codes are presentation-only and identical every run,
    # but they sit BETWEEN the count words and ` in <dur>s`, so the duration
    # rules below can't match until they're gone. Stripping them is semantics-
    # preserving and unblocks every count/duration pattern.
    (re.compile(r"\x1b\[[0-9;]*m"), ""),
    # ISO-8601 timestamp with optional fractional seconds + Z. GitLab CI
    # logs prefix every line with one of these — the biggest single
    # source of per-run drift in the prompt.
    (re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?"), "<TS>"),
    # GitLab section markers carry Unix epoch — collapse the epoch only.
    (re.compile(r"(section_(?:start|end)):\d{8,}:"), r"\1:<EPOCH>:"),
    # Docker image SHA-256 digest (64 lowercase hex chars).
    (re.compile(r"sha256:[0-9a-f]{64}"), "sha256:<SHA256>"),
    # GitLab runner instance ID, e.g. runner-sy3kr6y1-project-2-concurrent-0.
    (re.compile(r"runner-[a-z0-9]+-project-\d+-concurrent-\d+"), "runner-<ID>"),
    # GitLab runner system ID line. "system ID: s_<hex>".
    (re.compile(r"(system ID:\s*)s_[a-f0-9]+", re.IGNORECASE), r"\1s_<ID>"),
    # Gitaly correlation ID (Crockford base32 ULID, ~26 chars).
    (re.compile(r"(correlation ID:\s*)[0-9A-HJKMNP-TV-Z]{20,30}",
                re.IGNORECASE), r"\1<ID>"),
    # Detached HEAD commit SHA (anchored by surrounding context so we
    # don't strip 7-40 hex chars elsewhere — e.g. inside source code).
    (re.compile(r"(Checking out )[0-9a-f]{7,40}( as detached HEAD)"),
     r"\1<COMMIT>\2"),
    # pytest summary line duration. `2 failed, 2 passed in 0.04s` —
    # the 0.04s portion shifts between runs due to host load. Strip
    # only the duration; the outcome counts are SEMANTICALLY important
    # (different counts = different failure pattern) so they stay.
    # The word list pins to pytest's actual outcome words so this
    # doesn't accidentally match unrelated `N word in M.Ms` text.
    (re.compile(
        r"(\d+ (?:passed|failed|error|errors|skipped|warning|warnings|"
        r"deselected|xpassed|xfailed)"
        r"(?:,\s+\d+ (?:passed|failed|error|errors|skipped|warning|warnings|"
        r"deselected|xpassed|xfailed))* in )"
        r"\d+\.\d+(?:s|\s+seconds)\b"
     ), r"\1<DUR>s"),
    # Orchestrator-minted bug_id with optional urandom tail.
    # `2026_05_29-00_37_02_1_b10f` — leaks into retry_feedback paths
    # like `/tmp/dh_repo/<bug_id>/palindrome.py` and any error message
    # quoting the repo dir. Tight format anchor (date-with-decisecond
    # plus optional 4-hex tail) keeps this from matching arbitrary
    # underscored words. Matches the same shape the orchestrator
    # produces in `_handle_message`.
    (re.compile(r"\d{4}_\d{2}_\d{2}-\d{2}_\d{2}_\d{2}_\d(?:_[a-f0-9]{4})?"),
     "<BUG_ID>"),
    # ── volatiles observed leaking into mini's bash tool observations ────────
    # (SWE-bench agentic replay; each identified from the first cache-miss
    # divergence of a real ver99 replay — see docs/swebench.md.)
    #
    # Python object repr address: `<... object at 0x7fa5c4a19080>`,
    # `<SourceFileLoader object at 0x...>`. Any object without a custom
    # __repr__ prints its id() as a hex address that changes every process —
    # the single biggest divergence source when the agent `python -c`-prints
    # objects. Anchored on ` at 0x<hex>` (the CPython repr shape) so it can't
    # match a hex literal in source. The trailing `>` is kept.
    (re.compile(r" at 0x[0-9a-fA-F]+"), " at 0x<ADDR>"),
    # unittest / Django runtests summary duration: `Ran 164 tests in 0.411s`.
    # Same idea as the pytest-duration rule above but Django's test runner uses
    # unittest's format, not pytest's. The test COUNT is semantic (kept); only
    # the wallclock duration drifts with host load.
    (re.compile(r"(Ran \d+ tests? in )\d+\.\d+s"), r"\1<DUR>s"),
    # `grep -r` binary-file warning: `grep: <path>.pyc: binary file matches`.
    # These are grep machinery warnings (about compiled .pyc caches, not the
    # source), and `grep -r`'s filesystem traversal order isn't stable, so the
    # line lands at a different position between runs and busts an otherwise
    # identical result. Drop the whole line — it carries no fix-relevant
    # content. (Leading newline consumed so we don't leave a blank line.)
    (re.compile(r"\n?grep: [^\n]*: binary file matches"), ""),
    # pytest empty-selection summary: `==== no tests ran in 0.56s ====`. The
    # outcome-word duration rule above requires a count word (passed/failed/…);
    # "no tests ran" has none, so it drifts on duration alone (astropy/django
    # print it when a `-k`/path selection matches nothing).
    (re.compile(r"(no tests ran in )\d+\.\d+s"), r"\1<DUR>s"),
    # Random UUID4 (e.g. a model pk the agent prints): 8-4-4-4-12 hex. The shape
    # is specific enough to not hit real hex content.
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                r"[0-9a-fA-F]{12}\b"), "<UUID>"),
    # tempfile.mkdtemp / NamedTemporaryFile suffix: `/tmp/tmpXXXXXXXX`. Random
    # every run — django migration tests build a temp app package under it and
    # print its `_NamespacePath`.
    (re.compile(r"/tmp/tmp[A-Za-z0-9_]{6,}"), "/tmp/tmp<RAND>"),
    # `git stash pop` drop line: `Dropped refs/stash@{0} (<40-hex>)`. The stash
    # commit sha is new every run (the agent stashes/pops around its edits).
    (re.compile(r"(Dropped refs/stash@\{\d+\} \()[0-9a-f]{40}(\))"), r"\1<SHA>\2"),
    # `ls -l` mtime of container-created entries (`.`/`..`, temp dirs): the
    # `Mon DD HH:MM` field is the container build time, different every run.
    # Pinned to real month abbreviations so it can't match arbitrary text.
    (re.compile(r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) +"
                r"\d{1,2} +\d{2}:\d{2}\b"), "<LS_DATE>"),
)


def _normalize_volatile_content(text: str) -> str:
    """Strip per-run volatile metadata from a message content string so
    two runs of the same fixture (which produce DIFFERENT GitLab CI
    traces only in machinery metadata) hash to the same cache key.

    The deterministic part of the trace (pytest failures, file paths,
    exception types/messages, line numbers) is preserved unchanged.

    Patterns matched are documented in `_NORMALIZE_PATTERNS`. Anything
    not matched is left alone — conservative on purpose, since a
    too-broad match could feed the LLM a cached response that's
    semantically wrong for the new request.
    """
    for pattern, replacement in _NORMALIZE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _normalize_tools(tools: Any) -> list[Any]:
    """Tools are usually a list of function declarations. We sort by name
    so a config that re-orders declarations doesn't bust the cache, and we
    recursively normalize the JSON-Schema bodies via sort_keys at dump
    time."""
    if not isinstance(tools, list):
        return []
    out = []
    for t in tools:
        if isinstance(t, dict):
            out.append(t)
    # Sort by function.name when present so order is canonical.
    def _key(t: dict) -> str:
        fn = t.get("function")
        if isinstance(fn, dict):
            return str(fn.get("name", ""))
        return ""
    out.sort(key=_key)
    return out


def _normalize_params(body: dict[str, Any]) -> dict[str, Any]:
    """Pick the sampling/decoding params that affect the model's output.
    Missing fields stay missing — don't backfill defaults, because the
    backend's default may legitimately change between server versions and
    the cache key should reflect what the WORKER explicitly asked for."""
    out: dict[str, Any] = {}
    for k in _PARAM_FIELDS:
        if k in body:
            out[k] = body[k]
    return out


def derive_key(body: dict[str, Any], *, normalize_content: bool = False) -> str:
    """Return a hex SHA-256 cache key for the request body.

    Model name is included so a config change that swaps backends to a
    different model (even if the worker sends the same prompt) doesn't
    silently serve the wrong response.

    `normalize_content` (default False) toggles `_normalize_volatile_content`
    on every external message's string content — strips ISO-8601
    timestamps, runner IDs, Docker SHAs etc. from GitLab CI traces so
    two runs of the same fixture hash to the same key. Off by default
    to preserve byte-identical cache behaviour for non-stress-test
    callers.
    """
    payload = {
        "model":             body.get("model", ""),
        "external_messages": _normalize_messages(body.get("messages"),
                                                 normalize_content=normalize_content),
        "tools":             _normalize_tools(body.get("tools")),
        "params":            _normalize_params(body),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def short_key(full_key: str) -> str:
    """Display prefix used in log lines so cache events stay greppable
    without dumping the full 64-char hash. 12 chars = 48 bits, collision-
    safe for any realistic single-run set."""
    return full_key[:12]
