"""
services/prompt_guard.py

Prompt-injection defenses for content the LLM reads at runtime.

Threat model: CI traces, suspect-file content, files fetched mid-loop, and
the memory hint all originate from automated tools or external contributors.
A malicious actor (or even an honest comment / docstring) could contain text
that the LLM might mistake for instructions ("ignore previous instructions",
chat-template tokens, fake tool calls, ...).

Defenses provided here:
  - wrap_untrusted(text, label): wrap content in clear <<<UNTRUSTED:...>>>
    delimiters so the LLM has a syntactic boundary between task and data.
  - detect_injection(text, label): scan for known injection markers and
    return the matches. Intentionally log-only at call sites — real code
    can legitimately contain such strings (e.g. this very module's tests).

Pair this with the system-prompt [SECURITY] paragraph in react_loop, and
with the apply-time patch_guard. Neither layer alone is sufficient.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# Each entry is (name, compiled_pattern). Names are stable identifiers used
# in logs and tests so the set can evolve without breaking telemetry.
_INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "ignore_prior",
        re.compile(
            r"ignore\s+(?:all\s+|the\s+|any\s+|your\s+)?"
            r"(?:previous|prior|above|earlier|preceding)\s+"
            r"(?:instructions?|context|messages?|prompts?|rules?|directives?)",
            re.IGNORECASE,
        ),
    ),
    (
        "disregard_prior",
        re.compile(
            r"(?:disregard|forget)\s+(?:all\s+|the\s+|any\s+|your\s+)?"
            r"(?:previous|prior|above|earlier|preceding)",
            re.IGNORECASE,
        ),
    ),
    (
        "override_system",
        re.compile(
            r"(?:override|replace|update|change)\s+(?:the\s+|your\s+)?"
            r"system\s+(?:prompt|message|instructions?|directives?)",
            re.IGNORECASE,
        ),
    ),
    (
        "new_instructions",
        re.compile(
            r"new\s+(?:instructions?|system\s+prompt|task|directives?)\s*:",
            re.IGNORECASE,
        ),
    ),
    (
        "chat_token",
        re.compile(r"<\|(?:im_start|im_end|system|user|assistant|endoftext)\|>"),
    ),
    (
        "fake_tool_call",
        re.compile(
            r"<(?:tool_call|tool_calls|function_call|function_calls|invoke)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "forge_role",
        re.compile(r"^\s*(?:system|assistant)\s*:\s*\S", re.IGNORECASE | re.MULTILINE),
    ),
)


@dataclass(frozen=True)
class InjectionDetection:
    """One pattern match found in untrusted content."""

    pattern: str
    snippet: str
    source_label: str


def detect_injection(text: str, source_label: str) -> list[InjectionDetection]:
    """Scan `text` for known injection markers.

    Returns one InjectionDetection per matching pattern (first match per
    pattern). Empty list when nothing suspicious is found or `text` is empty.
    Caller decides what to do with the result — typical use is logging.
    """
    if not text:
        return []
    detections: list[InjectionDetection] = []
    for name, pat in _INJECTION_PATTERNS:
        m = pat.search(text)
        if m is not None:
            snippet = m.group(0)
            if len(snippet) > 120:
                snippet = snippet[:117] + "..."
            detections.append(
                InjectionDetection(pattern=name, snippet=snippet, source_label=source_label)
            )
    return detections


def wrap_untrusted(text: str, source_label: str) -> str:
    """Wrap `text` in clear UNTRUSTED delimiters.

    The label is included in both the open and close marker so that nested
    or adjacent untrusted blocks can be told apart.
    """
    safe_label = re.sub(r"[^A-Za-z0-9._:/\-]", "_", source_label)
    return (
        f"<<<UNTRUSTED:{safe_label}>>>\n"
        f"{text}\n"
        f"<<<END UNTRUSTED:{safe_label}>>>"
    )


def sanitize_untrusted(
    text: str, source_label: str, *, log: bool = True
) -> tuple[str, list[InjectionDetection]]:
    """Detect injection patterns and wrap in UNTRUSTED delimiters.

    Convenience for the common call site: detect + wrap + (optionally) log.
    Returns the wrapped text and the list of detections so the caller can
    add them to telemetry if desired.
    """
    detections = detect_injection(text, source_label)
    if log and detections:
        for d in detections:
            logger.warning(
                "prompt_guard: injection pattern %r in %s: %r",
                d.pattern, d.source_label, d.snippet,
            )
    return wrap_untrusted(text, source_label), detections
