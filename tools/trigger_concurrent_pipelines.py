#!/usr/bin/env python3
"""
tools/trigger_concurrent_pipelines.py — fire failed pipelines on multiple repos at once.

For each selected target project, POST /pipeline?ref=<ref> concurrently and
print one row per target. Trigger-only: returns immediately, does not poll
for resulting auto/bf/* MRs.

Target selection:
  --fixtures all|F01,F02,...    restrict to subset of repos under --prefix
                                inside --namespace (default: all). Match by
                                short id (F01) or full directory name
                                (F01-off-by-one).
  --extra-projects PATH,...     additional non-fixture projects to fire on.
                                Each entry is either a bare project name
                                (resolved as {namespace}/{name}) or a full
                                path_with_namespace (taken as-is). Useful
                                for mixing real-world repos with the
                                synthetic fixtures in one stress run.

Tuning:
  --concurrency N               size of the thread pool used for POSTs.
                                For a "strict N simultaneous webhooks"
                                stress test (the K8s memory-note pattern),
                                pick N == concurrency == len(targets) so
                                every POST is actually in flight at once;
                                with more targets than concurrency the
                                later POSTs wait for slots and the burst
                                is no longer strict.

Repetition (sustained / soak load):
  --repeat N                    fire the whole burst N times (default 1 =
                                the historical single-shot behaviour). Each
                                round re-triggers a pipeline on every target.
  --interval SECONDS            wait SECONDS between rounds (default 0;
                                ignored when --repeat 1). The sleep is
                                BETWEEN rounds only, never after the last, so
                                --repeat 3 --interval 60 spans ~120s + work.

Configuration is GitLab-/account-agnostic — pass --gitlab-url, --token,
--namespace, --prefix. Defaults match the bundled gitlab.com test
account (lishu20161 / sdlcma-fix-).

Example — two strict-simultaneous pipelines from two fixtures under the
self-hosted GitLab's root namespace:

  python tools/trigger_concurrent_pipelines.py \\
      --gitlab-url http://gitlab.local --namespace root \\
      --fixtures F01,F02 --concurrency 2

Example — eight strict-simultaneous on the bundled gitlab.com account:

  python tools/trigger_concurrent_pipelines.py \\
      --fixtures F01,F02,F03,F04,F05,F06,F07,F08 --concurrency 8

Example — soak: two pipelines every 60s for 10 rounds (~10 min):

  python tools/trigger_concurrent_pipelines.py \\
      --fixtures F01,F02 --concurrency 2 --repeat 10 --interval 60
"""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import sys
import time
import urllib.parse
from pathlib import Path

import requests

DEFAULT_GITLAB_URL = "https://gitlab.com"
DEFAULT_NAMESPACE = "lishu20161"
DEFAULT_PREFIX = "sdlcma-fix-"


def load_token_default() -> str | None:
    env = os.environ.get("GITLAB_PRIVATE_TOKEN")
    if env:
        return env
    settings_file = Path(__file__).resolve().parents[1] / "settings" / "worker_gitlab_saas.env"
    if settings_file.exists():
        for line in settings_file.read_text().splitlines():
            if line.startswith("GITLAB_PRIVATE_TOKEN="):
                return line.split("=", 1)[1].strip()
    return None


def list_fixture_projects(
    api: str, token: str, prefix: str, namespace: str | None = None
) -> list[dict]:
    # `owned=true` is unreliable on self-hosted Omnibus for the root admin
    # token — projects under root/ may not pass GitLab's "owned" predicate
    # even when the token belongs to root. Scope by namespace (when known)
    # via path_with_namespace instead; falls back to prefix-only on the
    # historical no-namespace call sites. Mirrors the equivalent function
    # in tools/gitlab_fixture_repos.py (commit 82908c3).
    r = requests.get(
        f"{api}/projects",
        headers={"PRIVATE-TOKEN": token},
        params={"search": prefix, "simple": "true", "per_page": 100},
        timeout=30,
    )
    r.raise_for_status()
    projs = [p for p in r.json() if p["path"].startswith(prefix)]
    if namespace:
        ns_prefix = f"{namespace}/"
        projs = [p for p in projs if p.get("path_with_namespace", "").startswith(ns_prefix)]
    return sorted(projs, key=lambda p: p["path"])


def filter_fixtures(projs: list[dict], wanted: list[str] | None, prefix: str) -> list[dict]:
    if not wanted:
        return projs
    wanted_upper = {w.upper() for w in wanted}
    out = []
    for p in projs:
        rest = p["path"][len(prefix):]
        short = rest.split("-", 1)[0]
        if short.upper() in wanted_upper or rest in wanted or rest.upper() in wanted_upper:
            out.append(p)
    return out


def resolve_extra_projects(entries: list[str], namespace: str | None) -> list[str]:
    """Turn each --extra-projects entry into a full path_with_namespace.

    Bare names (no slash) resolve as ``{namespace}/{name}`` — the common
    case (e.g. ``--extra-projects order_be``). Entries that already carry
    a slash are taken as-is, so cross-namespace targets stay possible
    (``--extra-projects other-group/their-repo``).
    """
    resolved: list[str] = []
    for entry in entries:
        e = entry.strip()
        if not e:
            continue
        if "/" in e:
            resolved.append(e)
        else:
            if not namespace:
                raise SystemExit(
                    f"--extra-projects entry {e!r} is a bare name but no --namespace was given"
                )
            resolved.append(f"{namespace}/{e}")
    return resolved


def fetch_project_id(api: str, token: str, path_with_namespace: str) -> int:
    enc = urllib.parse.quote(path_with_namespace, safe="")
    r = requests.get(
        f"{api}/projects/{enc}",
        headers={"PRIVATE-TOKEN": token},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["id"]


def trigger_pipeline(api: str, token: str, project_id: int, ref: str):
    try:
        r = requests.post(
            f"{api}/projects/{project_id}/pipeline",
            headers={"PRIVATE-TOKEN": token},
            params={"ref": ref},
            timeout=30,
        )
        if r.status_code in (200, 201):
            return r.json().get("id"), r.status_code, ""
        return None, r.status_code, r.text[:200]
    except Exception as e:
        return None, 0, repr(e)


def run_round(api, token, targets, ref, concurrency, round_label=""):
    """Fire one concurrent burst at every target, print a result table, and
    return ``(ok_count, total, elapsed_s)``. One round = one pipeline POST
    per target; the repeat loop in main() calls this N times."""
    prefix = f"[{round_label}] " if round_label else ""
    print(
        f"{prefix}triggering {len(targets)} pipeline(s) @ ref={ref} "
        f"concurrency={concurrency}"
    )
    t0 = time.time()
    results: list[tuple[str, int | None, int, str]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as ex:
        futs = {
            ex.submit(trigger_pipeline, api, token, pid, ref): path
            for path, pid in targets
        }
        for f in concurrent.futures.as_completed(futs):
            path = futs[f]
            pid, status, err = f.result()
            results.append((path, pid, status, err))

    elapsed = time.time() - t0
    results.sort(key=lambda r: r[0])
    print(f"\n{'project':50s} {'status':>7} {'pipeline':>10}  notes")
    print("-" * 90)
    ok = 0
    for path, pid, status, err in results:
        marker = "OK " if pid else "ERR"
        msg = err[:30] if err else ""
        print(f"{path:50s} {status:>7} {str(pid or '-'):>10}  {marker} {msg}")
        if pid:
            ok += 1
    print("-" * 90)
    print(f"{ok}/{len(results)} triggered in {elapsed:.2f}s")
    return ok, len(results), elapsed


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Concurrently trigger pipelines for SDLCMA smoke testing"
    )
    p.add_argument("--gitlab-url", default=DEFAULT_GITLAB_URL)
    p.add_argument("--token", default=None)
    p.add_argument("--namespace", default=DEFAULT_NAMESPACE)
    p.add_argument("--prefix", default=DEFAULT_PREFIX)
    p.add_argument(
        "--fixtures", default="all", help="all | comma list (F01,F02 or F01-off-by-one,...)"
    )
    p.add_argument(
        "--extra-projects",
        default="",
        help="comma list of additional projects to fire on; bare names "
             "resolve as {namespace}/{name}, slash-bearing entries are "
             "taken as full path_with_namespace",
    )
    p.add_argument("--ref", default="main")
    p.add_argument("--concurrency", type=int, default=5)
    p.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="number of times to fire the whole burst (default 1). Each "
             "round re-triggers a pipeline on every target.",
    )
    p.add_argument(
        "--interval",
        type=float,
        default=0.0,
        help="seconds to wait between rounds (default 0; ignored when "
             "--repeat 1). Sleep is between rounds only, not after the last.",
    )
    args = p.parse_args(argv)

    if args.repeat < 1:
        raise SystemExit("--repeat must be >= 1")
    if args.interval < 0:
        raise SystemExit("--interval must be >= 0")

    if args.token is None:
        args.token = load_token_default()
    if not args.token:
        raise SystemExit("no token: pass --token or set GITLAB_PRIVATE_TOKEN")

    api = f"{args.gitlab_url}/api/v4"
    wanted = None
    if args.fixtures and args.fixtures.lower() != "all":
        wanted = [s.strip() for s in args.fixtures.split(",") if s.strip()]

    targets: list[tuple[str, int]] = []
    fixture_projs = list_fixture_projects(api, args.token, args.prefix, args.namespace)
    for fp in filter_fixtures(fixture_projs, wanted, args.prefix):
        targets.append((fp["path_with_namespace"], fp["id"]))

    extra_entries = [s for s in (args.extra_projects or "").split(",") if s.strip()]
    for path in resolve_extra_projects(extra_entries, args.namespace):
        targets.append((path, fetch_project_id(api, args.token, path)))

    if not targets:
        raise SystemExit("no targets matched")

    total_ok = 0
    total_triggered = 0
    for i in range(args.repeat):
        label = f"round {i + 1}/{args.repeat}" if args.repeat > 1 else ""
        if label:
            print(f"\n===== {label} =====")
        ok, total, _ = run_round(
            api, args.token, targets, args.ref, args.concurrency, label
        )
        total_ok += ok
        total_triggered += total
        if i < args.repeat - 1 and args.interval > 0:
            print(f"\nsleeping {args.interval:g}s before next round...")
            time.sleep(args.interval)

    if args.repeat > 1:
        print(
            f"\n===== total: {total_ok}/{total_triggered} triggered across "
            f"{args.repeat} round(s) ====="
        )
    sys.exit(0 if total_ok == total_triggered else 2)


if __name__ == "__main__":
    main()
