#!/usr/bin/env python3
"""
tools/trigger_concurrent_pipelines.py — fire failed pipelines on multiple repos at once.

For each selected target project, POST /pipeline?ref=main concurrently and
print one row per target. Trigger-only: returns immediately, does not poll
for resulting auto/bf/* MRs.

Target selection:
  --fixtures all|F01,F02,...   restrict to subset of repos under --prefix
                               (default: all). Match by short id (F01) or
                               full directory name (F01-off-by-one).
  --include-order-be           also fire on lishu20161/order_be (or
                               override the path via --order-be-path).

Tuning:
  --concurrency 5              size of the thread pool used for POSTs.
                               Default 5 keeps simultaneous worker spawns
                               manageable on a single t3.micro; raise for
                               stress testing.

Configuration is GitLab-/account-agnostic — pass --gitlab-url, --token,
--namespace, --prefix. Defaults match the bundled gitlab.com test account.
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
DEFAULT_PREFIX = "sdlcma-fix-"
DEFAULT_ORDER_BE = "lishu20161/order_be"


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


def list_fixture_projects(api: str, token: str, prefix: str) -> list[dict]:
    r = requests.get(
        f"{api}/projects",
        headers={"PRIVATE-TOKEN": token},
        params={"search": prefix, "owned": "true", "simple": "true", "per_page": 100},
        timeout=30,
    )
    r.raise_for_status()
    return sorted(
        (p for p in r.json() if p["path"].startswith(prefix)),
        key=lambda p: p["path"],
    )


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


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Concurrently trigger pipelines for SDLCMA smoke testing"
    )
    p.add_argument("--gitlab-url", default=DEFAULT_GITLAB_URL)
    p.add_argument("--token", default=None)
    p.add_argument("--prefix", default=DEFAULT_PREFIX)
    p.add_argument(
        "--fixtures", default="all", help="all | comma list (F01,F02 or F01-off-by-one,...)"
    )
    p.add_argument("--include-order-be", action="store_true")
    p.add_argument("--order-be-path", default=DEFAULT_ORDER_BE)
    p.add_argument("--ref", default="main")
    p.add_argument("--concurrency", type=int, default=5)
    args = p.parse_args(argv)

    if args.token is None:
        args.token = load_token_default()
    if not args.token:
        raise SystemExit("no token: pass --token or set GITLAB_PRIVATE_TOKEN")

    api = f"{args.gitlab_url}/api/v4"
    wanted = None
    if args.fixtures and args.fixtures.lower() != "all":
        wanted = [s.strip() for s in args.fixtures.split(",") if s.strip()]

    targets: list[tuple[str, int]] = []
    fixture_projs = list_fixture_projects(api, args.token, args.prefix)
    for fp in filter_fixtures(fixture_projs, wanted, args.prefix):
        targets.append((fp["path_with_namespace"], fp["id"]))

    if args.include_order_be:
        enc = urllib.parse.quote(args.order_be_path, safe="")
        r = requests.get(
            f"{api}/projects/{enc}",
            headers={"PRIVATE-TOKEN": args.token},
            timeout=30,
        )
        r.raise_for_status()
        targets.append((args.order_be_path, r.json()["id"]))

    if not targets:
        raise SystemExit("no targets matched")

    print(
        f"triggering {len(targets)} pipeline(s) @ ref={args.ref} concurrency={args.concurrency}"
    )
    t0 = time.time()
    results: list[tuple[str, int | None, int, str]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futs = {
            ex.submit(trigger_pipeline, api, args.token, pid, args.ref): path
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
    sys.exit(0 if ok == len(results) else 2)


if __name__ == "__main__":
    main()
