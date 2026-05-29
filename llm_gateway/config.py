"""Gateway config loader.

A config file declares one or more `backends` and one `inference_policy`.
Format is YAML (preferred) or JSON — both parse to the same dict shape.

Shape:

    backends:
      - name: <unique-id>
        base_url: <OpenAI-compatible /v1 endpoint>
        model: <model name to forward upstream>
        api_key: <literal>           # OR
        api_key_env: <env-var-name>  # read at load time
        request_timeout: <seconds, default 600>

    inference_policy:
      type: ordered                  # only type supported today
      order: [<backend-name>, ...]   # subset of `backends[*].name`; primary first
      advance_on:
        fix_failure: true|false      # advance on X-Sdlcma-Attempt > prev
        backend_error: true|false    # advance when upstream is unreachable / 5xx

Validation aborts the gateway process at startup — failing late means a
worker run silently goes to the wrong backend, which contaminates eval
results. Same discipline as `llm_model_check`.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class BackendConfig:
    name: str
    base_url: str
    model: str
    api_key: str
    request_timeout: float = 600.0


@dataclass(frozen=True)
class InferencePolicyConfig:
    type: str
    order: tuple[str, ...]
    advance_on_fix_failure: bool


@dataclass(frozen=True)
class CacheConfig:
    """Optional response cache, see llm_gateway/cache.py.

    mode:
      * disabled — pass-through, no cache touched (the safe default).
      * record   — pass-through, but persist each successful response.
                   Use during the first "warm" run that you want to
                   replay later.
      * replay   — strict cache-only. On miss, return 409 (not 502) so
                   the worker can tell "cache gap" apart from "backend
                   down". Use for deterministic regression replays.
      * cache    — replay on hit, record on miss. Use for stress tests
                   where the first request fills the cache and every
                   subsequent burst costs zero tokens.

    db_path is the sqlite file. One file is the whole cache → trivially
    copyable across machines (rsync / scp / cp), which is the intended
    sharing model.

    replay_with_latency: when True, a cache hit sleeps to the original
    recorded wallclock before returning. Keeps stress-test phase-3
    numbers realistic even though no token was spent.

    log_every_n: emit a one-line `phase_marker phase=cache_summary` log
    every N requests so a long-running run leaves a trail without
    flooding logs. 0 = never (rely on /cache/stats polling instead).
    """
    enabled: bool = False
    mode: str = "disabled"
    db_path: str = ""
    replay_with_latency: bool = False
    log_every_n: int = 100
    # When True, the cache key strips per-run volatile patterns from
    # message content (ISO-8601 timestamps, runner IDs, Docker SHAs,
    # commit SHAs in detached-HEAD lines, etc.) before hashing — so two
    # runs of the same fixture hash to the same key even though the
    # GitLab CI trace machinery metadata differs. Off by default
    # because it's only relevant for stress-test cacheability and
    # could theoretically mis-normalise a fixture whose semantic
    # content matches one of the volatile patterns. Turn on per
    # `cache.normalize_content: true` in the YAML config.
    normalize_content: bool = False


_CACHE_MODES = ("disabled", "record", "replay", "cache")


@dataclass(frozen=True)
class GatewayConfig:
    backends: tuple[BackendConfig, ...]
    policy: InferencePolicyConfig
    backends_by_name: dict[str, BackendConfig] = field(default_factory=dict)
    cache: CacheConfig = field(default_factory=CacheConfig)


class GatewayConfigError(ValueError):
    """Raised on any structural problem in the config file."""


def _read_yaml_or_json(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore[import-not-found]
        except ImportError as exc:
            raise GatewayConfigError(
                f"yaml config {path} needs PyYAML; install it or use a .json file"
            ) from exc
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    if not isinstance(data, dict):
        raise GatewayConfigError(f"{path}: top-level must be a mapping, got {type(data).__name__}")
    return data


def _resolve_api_key(raw: dict[str, Any], backend_name: str) -> str:
    if "api_key" in raw and "api_key_env" in raw:
        raise GatewayConfigError(
            f"backend {backend_name!r}: set api_key OR api_key_env, not both"
        )
    if "api_key" in raw:
        key = raw["api_key"]
        if not isinstance(key, str) or not key:
            raise GatewayConfigError(f"backend {backend_name!r}: api_key must be a non-empty string")
        return key
    if "api_key_env" in raw:
        env_name = raw["api_key_env"]
        if not isinstance(env_name, str) or not env_name:
            raise GatewayConfigError(
                f"backend {backend_name!r}: api_key_env must be a non-empty string"
            )
        val = os.environ.get(env_name)
        if not val:
            raise GatewayConfigError(
                f"backend {backend_name!r}: env var {env_name!r} is not set"
            )
        return val
    # Self-hosted convention: empty key (vLLM community default). The openai
    # client rejects literal "" so we substitute the project's "EMPTY" sentinel.
    return "EMPTY"


def _parse_backend(raw: Any) -> BackendConfig:
    if not isinstance(raw, dict):
        raise GatewayConfigError(f"backend entries must be mappings, got {type(raw).__name__}")
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise GatewayConfigError("backend.name is required and must be a non-empty string")
    base_url = raw.get("base_url")
    if not isinstance(base_url, str) or not base_url:
        raise GatewayConfigError(f"backend {name!r}: base_url is required")
    model = raw.get("model")
    if not isinstance(model, str) or not model:
        raise GatewayConfigError(f"backend {name!r}: model is required")
    timeout = raw.get("request_timeout", 600)
    try:
        timeout_f = float(timeout)
    except (TypeError, ValueError) as exc:
        raise GatewayConfigError(
            f"backend {name!r}: request_timeout must be numeric, got {timeout!r}"
        ) from exc
    return BackendConfig(
        name=name,
        base_url=base_url.rstrip("/"),
        model=model,
        api_key=_resolve_api_key(raw, name),
        request_timeout=timeout_f,
    )


def _parse_policy(raw: Any, known_names: set[str]) -> InferencePolicyConfig:
    if not isinstance(raw, dict):
        raise GatewayConfigError(
            f"inference_policy must be a mapping, got {type(raw).__name__}"
        )
    ptype = raw.get("type", "ordered")
    if ptype != "ordered":
        # Future: "cost_aware", "round_robin", "heuristic". One implementation
        # only today — declaring an unsupported type early is safer than
        # accepting it and degrading silently to ordered.
        raise GatewayConfigError(
            f"inference_policy.type={ptype!r} not supported; only 'ordered' implemented"
        )
    order = raw.get("order")
    if not isinstance(order, list) or not order:
        raise GatewayConfigError(
            "inference_policy.order is required and must be a non-empty list"
        )
    seen: set[str] = set()
    for n in order:
        if not isinstance(n, str):
            raise GatewayConfigError(f"inference_policy.order entries must be strings; got {n!r}")
        if n not in known_names:
            raise GatewayConfigError(
                f"inference_policy.order references unknown backend {n!r}; "
                f"known: {sorted(known_names)}"
            )
        if n in seen:
            raise GatewayConfigError(f"inference_policy.order has duplicate entry {n!r}")
        seen.add(n)
    advance_on = raw.get("advance_on", {}) or {}
    if not isinstance(advance_on, dict):
        raise GatewayConfigError("inference_policy.advance_on must be a mapping")
    # `backend_error` is accepted for backward compatibility with configs
    # written before the policy went stateless; it is logged-and-ignored.
    # Reactive backend-error advance is reserved for a future process-wide
    # circuit breaker (out of scope today — see docstring in policy.py).
    if "backend_error" in advance_on:
        import logging as _logging
        _logging.getLogger(__name__).info(
            "inference_policy.advance_on.backend_error is accepted but ignored "
            "(policy is stateless; circuit-breaker is future work)"
        )
    return InferencePolicyConfig(
        type="ordered",
        order=tuple(order),
        advance_on_fix_failure=bool(advance_on.get("fix_failure", True)),
    )


def _parse_cache(raw: Any) -> CacheConfig:
    """Optional. Missing/None → cache disabled (the safe legacy behavior).

    Validating modes up-front matters because a typo (`replat`) shouldn't
    silently degrade to pass-through and cost real tokens during a run
    that was intended to be free.
    """
    if raw is None:
        return CacheConfig()
    if not isinstance(raw, dict):
        raise GatewayConfigError(
            f"cache: must be a mapping when present, got {type(raw).__name__}"
        )
    mode = str(raw.get("mode", "disabled")).lower()
    if mode not in _CACHE_MODES:
        raise GatewayConfigError(
            f"cache.mode={mode!r} unsupported; pick one of {list(_CACHE_MODES)}"
        )
    enabled = mode != "disabled"
    db_path = raw.get("db_path", "")
    if enabled and (not isinstance(db_path, str) or not db_path):
        raise GatewayConfigError(
            f"cache.mode={mode!r} requires cache.db_path (path to a sqlite file)"
        )
    log_every_n = raw.get("log_every_n", 100)
    try:
        log_every_n_i = int(log_every_n)
    except (TypeError, ValueError) as exc:
        raise GatewayConfigError(
            f"cache.log_every_n must be an integer, got {log_every_n!r}"
        ) from exc
    if log_every_n_i < 0:
        raise GatewayConfigError("cache.log_every_n must be >= 0")
    return CacheConfig(
        enabled=enabled,
        mode=mode,
        db_path=db_path,
        replay_with_latency=bool(raw.get("replay_with_latency", False)),
        log_every_n=log_every_n_i,
        normalize_content=bool(raw.get("normalize_content", False)),
    )


def load_config(path: str | Path) -> GatewayConfig:
    """Parse a gateway config file. Raises GatewayConfigError on any problem."""
    p = Path(path)
    if not p.is_file():
        raise GatewayConfigError(f"gateway config file not found: {p}")
    data = _read_yaml_or_json(p)
    raw_backends = data.get("backends")
    if not isinstance(raw_backends, list) or not raw_backends:
        raise GatewayConfigError("`backends` is required and must be a non-empty list")
    backends = tuple(_parse_backend(b) for b in raw_backends)
    names = [b.name for b in backends]
    if len(names) != len(set(names)):
        dupes = sorted({n for n in names if names.count(n) > 1})
        raise GatewayConfigError(f"duplicate backend names: {dupes}")
    policy = _parse_policy(data.get("inference_policy"), set(names))
    cache = _parse_cache(data.get("cache"))
    by_name = {b.name: b for b in backends}
    cfg = GatewayConfig(
        backends=backends, policy=policy, backends_by_name=by_name, cache=cache,
    )
    return cfg
