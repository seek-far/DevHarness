"""
Memory enhancement — surface prior fix patterns to the ReAct loop.

Two callbacks:
  - lookup  (PRE_REACT_LOOP)  — query store, set state["memory_hint"].
  - writer  (AGENT_POST_FIX)  — append this run's outcome to the store.

The store is a JSON file: {"entries": [ {bug_id, outcome, error_signature,
suspect_file_path, fix_summary, category, timestamp}, ... ]}.

Lookup is keyword-matched against `error_info` and `suspect_file_path` — cheap,
no embeddings, good enough for the bundled fixtures and easy to reason about.
The store can be pre-seeded with curated lessons so the first sweep has
something to retrieve; live runs append further entries when the writer is
registered.
"""

from __future__ import annotations
import json
import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_TOP_K = 2
_STOPWORDS = {
    "the", "a", "an", "is", "in", "on", "at", "for", "to", "of", "and", "or",
    "but", "with", "as", "by", "if", "it", "this", "that", "from", "be",
    "was", "were", "are", "not", "no", "yes",
}
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


def _tokenize(text: str) -> set[str]:
    if not text:
        return set()
    return {t.lower() for t in _TOKEN_RE.findall(text) if t.lower() not in _STOPWORDS}


# ── store ───────────────────────────────────────────────────────────────────


class MemoryStore:
    """Append-mostly JSON-backed store of past fix attempts.

    File access is guarded by a process-local lock; the store is small enough
    that we just rewrite the whole file on each append.
    """

    _LOCK = threading.Lock()

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("memory: store read failed at %s: %s", self.path, exc)
            return []
        if isinstance(data, dict):
            return list(data.get("entries", []))
        return list(data) if isinstance(data, list) else []

    def append(self, entry: dict) -> None:
        with self._LOCK:
            entries = self.load()
            entries.append(entry)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps({"entries": entries}, indent=2, default=str),
                encoding="utf-8",
            )

    def query(self, error_info: str, suspect_file_path: str, top_k: int) -> list[dict]:
        """Return up to top_k entries with non-zero token overlap, highest score first."""
        entries = self.load()
        if not entries:
            return []
        query_tokens = _tokenize(error_info) | _tokenize(suspect_file_path)
        if not query_tokens:
            return []
        scored: list[tuple[int, dict]] = []
        for e in entries:
            entry_text = " ".join([
                str(e.get("error_signature", "")),
                str(e.get("suspect_file_path", "")),
                str(e.get("category", "")),
                str(e.get("fix_summary", "")),
            ])
            entry_tokens = _tokenize(entry_text)
            if not entry_tokens:
                continue
            score = len(query_tokens & entry_tokens)
            if score > 0:
                scored.append((score, e))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [e for _, e in scored[:top_k]]


# ── hint formatting ─────────────────────────────────────────────────────────


def _format_hint(matches: list[dict]) -> str:
    lines = ["## Prior similar fixes (reference only, may not apply):"]
    for i, m in enumerate(matches, 1):
        sig = m.get("error_signature", "").strip()
        cat = m.get("category", "").strip()
        fix = m.get("fix_summary", "").strip()
        head = f"{i}. "
        if cat:
            head += f"[{cat}] "
        if sig:
            head += sig
        lines.append(head)
        if fix:
            lines.append(f"   fix: {fix}")
    return "\n".join(lines)


# ── callbacks ───────────────────────────────────────────────────────────────


def make_memory_lookup(store: MemoryStore, top_k: int = DEFAULT_TOP_K):
    """Build a PRE_REACT_LOOP callback that injects state['memory_hint']."""

    def memory_lookup(state: dict) -> dict | None:
        error_info = state.get("error_info") or ""
        suspect = state.get("suspect_file_path") or ""
        matches = store.query(error_info, suspect, top_k)
        if not matches:
            return None
        hint = _format_hint(matches)
        logger.info("memory: %d match(es) injected into react_loop", len(matches))
        return {"memory_hint": hint, "memory_matches_count": len(matches)}

    memory_lookup.__name__ = "memory_lookup"
    return memory_lookup


def make_memory_writer(store: MemoryStore, max_summary_chars: int = 240):
    """Build an AGENT_POST_FIX callback that appends this run's outcome to the store."""

    def memory_writer(state: dict) -> dict | None:
        error_info = (state.get("error_info") or "").strip()
        suspect = state.get("suspect_file_path") or ""
        outcome = "fixed" if state.get("test_passed") else (
            "error" if state.get("error") else "no_fix"
        )

        # Distill a short fix summary from react_reasoning or first fix patch line.
        reasoning = (state.get("react_reasoning") or "").strip()
        fix_summary = reasoning[:max_summary_chars]
        if not fix_summary:
            llm_result = state.get("llm_result") or {}
            fixes = llm_result.get("fixes") or []
            if fixes:
                first = fixes[0]
                fix_summary = (
                    f"{first.get('original','')[:80]} -> {first.get('replacement','')[:80]}"
                )

        # Reduce error_info to a 1-line signature.
        # Prefer the pytest "E   ..." line (the actual error). Fall back to
        # the first non-banner non-empty line. Skip banners that are runs of
        # '=' or '_' (pytest section dividers).
        signature = ""
        candidates = []
        for line in error_info.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if set(stripped) <= {"=", "_", " "}:
                continue
            candidates.append(stripped)
        for line in candidates:
            if line.startswith("E "):
                signature = line.lstrip("E ").strip()
                break
        if not signature and candidates:
            signature = candidates[0]
        signature = signature[:max_summary_chars]

        entry = {
            "bug_id":           state.get("bug_id", ""),
            "outcome":          outcome,
            "error_signature":  signature,
            "suspect_file_path": suspect,
            "fix_summary":      fix_summary,
            "timestamp":        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        }
        try:
            store.append(entry)
            logger.info("memory: wrote entry bug=%s outcome=%s",
                        entry["bug_id"], entry["outcome"])
        except Exception as exc:
            logger.warning("memory: append failed (non-fatal): %s", exc)
        return None

    memory_writer.__name__ = "memory_writer"
    return memory_writer


# ── builder used by the agent factory ───────────────────────────────────────


def build_memory_callbacks(
    store_path: str | Path,
    top_k: int = DEFAULT_TOP_K,
    write_back: bool = True,
) -> list[tuple[str, Any]]:
    """Return (hook_name, callback) tuples ready for LangGraphAgent."""
    from enhancements.hooks import HookName  # local import to avoid cycles

    store = MemoryStore(Path(store_path))
    callbacks: list[tuple[str, Any]] = [
        (HookName.PRE_REACT_LOOP, make_memory_lookup(store, top_k=top_k)),
    ]
    if write_back:
        callbacks.append((HookName.AGENT_POST_FIX, make_memory_writer(store)))
    return callbacks
