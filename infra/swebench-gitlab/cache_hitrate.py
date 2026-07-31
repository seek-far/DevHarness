"""Per-instance LLM-cache hit rate for one or more L2c/L3 arms.

Why per-instance and not overall: an arm's *aggregate* hit rate says nothing
about whether a given instance is comparable across arms. One miss forks that
instance's trajectory, and everything after it in that run is a different
conversation — so the instance stops being evidence for or against anything.
The judgeable population is exactly "the instances that replayed fully in every
arm", and this script computes it.

The gateway logs one line per lookup, tagged with the instance:

    phase_marker phase=cache_lookup mode=cache result=hit key=… bug_id=<instance_id>

usage:
    cache_hitrate.py --arms A A2 C [--clean-subset clean.txt]
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os
import re
from pathlib import Path

_LINE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ .*?"
    r"phase=cache_lookup .*?result=(?P<result>\w+).*?bug_id=(?P<iid>\S+)")


def parse_log(text: str) -> list[tuple[float, str, str]]:
    """[(epoch, result, instance_id)] for every cache lookup in the log."""
    out = []
    for line in text.splitlines():
        m = _LINE.match(line)
        if not m:
            continue
        try:
            ts = dt.datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S").timestamp()
        except ValueError:
            continue
        out.append((ts, m.group("result"), m.group("iid")))
    return out


def per_instance(lookups, start: float, end: float) -> dict[str, dict]:
    """instance -> {hit, miss, total, rate} inside one arm's window."""
    acc: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for ts, result, iid in lookups:
        # The log has second resolution; widen by a second on each side rather
        # than silently dropping the first/last call of an arm.
        if start - 1 <= ts <= end + 1:
            acc[iid][result] += 1
    stats = {}
    for iid, c in acc.items():
        total = sum(c.values())
        stats[iid] = {"hit": c["hit"], "miss": c["miss"], "total": total,
                      "rate": (c["hit"] / total) if total else 0.0}
    return stats


def clean_subset(per_arm: dict[str, dict[str, dict]]) -> list[str]:
    """Instances that replayed 100% in EVERY arm — the only comparable ones."""
    arms = list(per_arm)
    if not arms:
        return []
    common = set(per_arm[arms[0]])
    for a in arms[1:]:
        common &= set(per_arm[a])
    return sorted(i for i in common
                  if all(per_arm[a][i]["total"] and per_arm[a][i]["miss"] == 0 for a in arms))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arms", nargs="+", required=True)
    ap.add_argument("--w2-dir", default="~/.sdlcma/w2")
    ap.add_argument("--log", default="")
    ap.add_argument("--clean-subset", default="", help="write the judgeable instance list here")
    ap.add_argument("--per-instance", action="store_true", help="print every instance, not just misses")
    args = ap.parse_args()

    w2 = Path(os.path.expanduser(args.w2_dir))
    log = Path(os.path.expanduser(args.log or (w2 / "logs" / "llmgw.log")))
    lookups = parse_log(log.read_text(errors="ignore"))
    print(f"{len(lookups)} cache lookups in {log}")

    per_arm = {}
    for arm in args.arms:
        meta = json.loads((w2 / "arms" / f"{arm}.json").read_text())
        stats = per_instance(lookups, meta["start"], meta["end"])
        per_arm[arm] = stats
        hit = sum(s["hit"] for s in stats.values())
        total = sum(s["total"] for s in stats.values())
        dirty = sorted(i for i, s in stats.items() if s["miss"])
        print(f"\narm {arm}: {len(stats)} instances, {hit}/{total} requests hit "
              f"({100*hit/total if total else 0:.1f}%), {len(dirty)} instance(s) with misses")
        for iid in dirty:
            s = stats[iid]
            print(f"    {iid:45} hit={s['hit']:>4} miss={s['miss']:>4} rate={100*s['rate']:5.1f}%")
        if args.per_instance:
            for iid in sorted(stats):
                s = stats[iid]
                print(f"  · {iid:45} hit={s['hit']:>4} miss={s['miss']:>4}")

    clean = clean_subset(per_arm)
    union = set().union(*(set(s) for s in per_arm.values())) if per_arm else set()
    print(f"\njudgeable subset (100% replay in every arm): {len(clean)} / {len(union)} instances")
    if args.clean_subset:
        Path(os.path.expanduser(args.clean_subset)).write_text("\n".join(clean) + "\n")
        print(f"written to {args.clean_subset}")


if __name__ == "__main__":
    main()
