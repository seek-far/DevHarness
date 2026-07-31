"""L2c assertions: read one chaos arm + one control arm and decide pass/fail.

The six things L2c is for (design §11) are the ones a single-instance test
cannot reach, and every one of them is a *concurrency* property:

  1. records stay in their own `<run_key>/` sub-directory,
  2. a resumed run re-attaches to the container **it** started — never a
     neighbour's (attaching to the wrong container is the most destructive
     failure this feature can have),
  3. a killed instance still produces the control arm's patch,
  4. every run's records are purged at the end (the release path does not leak
     under load),
  5. no eval containers are left behind,
  6. peak disk of the checkpoint directory is the order of magnitude claimed.

Inputs are the chaos driver's JSONL, the journal, and the orchestrator log
(worker stdout is inherited by the orchestrator, so the `resume: attached to
container <cid> (key=<bug_id>...)` lines land there).

usage:  verify_l2c.py --control A --chaos C [--w2-dir ~/.sdlcma/w2]
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import time
from pathlib import Path

_ATTACH = re.compile(r"resume: attached to container (\w+) \(key=([\w.-]+), marker=(\w+)\)")

OK, BAD, SKIP = "PASS", "FAIL", "SKIP"


# ── the patch under comparison ───────────────────────────────────────────────
#
# `model_patch` is NOT a RunRecord field — it lives in graph state and never
# reaches the journal. Reading `record["model_patch"]` therefore yields "" for
# every run, and comparing "" to "" reports a cheerful PASS no matter what the
# agent produced. The real artefact is the pushed fix branch, so we ask GitLab
# for its diff against the instance's base branch. If that cannot be fetched the
# check reports SKIP — an unavailable comparison must never look like a passing
# one.

def project_path(record: dict) -> str | None:
    """`http://host/swebench/astropy` -> `swebench/astropy`."""
    url = record.get("project_web_url") or ""
    m = re.match(r"https?://[^/]+/(.+?)/?$", url)
    return m.group(1) if m else None


def normalized_diff(payload: dict) -> str:
    """Order-independent text form of GitLab's compare response."""
    parts = []
    for d in sorted(payload.get("diffs") or [], key=lambda x: (x.get("new_path") or "",
                                                               x.get("old_path") or "")):
        parts.append(f"--- {d.get('old_path')}\n+++ {d.get('new_path')}\n{d.get('diff') or ''}")
    return "\n".join(parts)


def fetch_fix_diff(record: dict, api: str, token: str, timeout: int = 30) -> str | None:
    """The diff the run actually produced, or None if it cannot be determined."""
    import urllib.parse
    import urllib.request

    path = project_path(record)
    branch = record.get("commit_branch") or record.get("fix_branch_name")
    base = record.get("base_branch")
    if not (path and branch and base and api and token):
        return None
    url = (f"{api.rstrip('/')}/projects/{urllib.parse.quote(path, safe='')}"
           f"/repository/compare?from={urllib.parse.quote(base, safe='')}"
           f"&to={urllib.parse.quote(branch, safe='')}&straight=true")
    req = urllib.request.Request(url, headers={"PRIVATE-TOKEN": token})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return normalized_diff(json.loads(r.read().decode()))
    except Exception:
        return None


def make_patch_fetcher(api: str, token: str, cache: dict[str, str],
                       cache_path: "Path | None", delay: float = 0.0,
                       fetch=None):
    """`record -> patch text | None`, cached across arms and paced.

    Both knobs exist because of the same operational fact: GitLab is typically
    reachable only from the host that ran the sweep, and every call makes gitaly
    diff two refs of a large repo. 2N of them back-to-back is a disk-saturating
    burst; caching removes the repeats across the two verdict tables and lets a
    killed run resume, pacing keeps the rest from arriving all at once.
    """
    fetch = fetch or fetch_fix_diff

    def patch_of(rec: dict) -> str | None:
        key = f"{rec.get('bug_id')}:{rec.get('commit_branch')}"
        if key in cache:
            return cache[key]               # a cache hit costs the server nothing
        if delay:
            time.sleep(delay)
        diff = fetch(rec, api, token)
        # A failed fetch is NOT cached: it means "GitLab was unreachable from
        # here", not "this run produced no patch". Persisting it would freeze a
        # transient outage into a permanent SKIP on every later re-run.
        if diff is not None:
            cache[key] = diff
            save_diff_cache(cache_path, cache)
        return diff

    return patch_of


def load_diff_cache(path: Path | None) -> dict[str, str]:
    if path is None or not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def save_diff_cache(path: Path | None, cache: dict[str, str]) -> None:
    """Write after every new patch — the point is to survive a mid-run death."""
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(cache))
    tmp.replace(path)


# ── loading ──────────────────────────────────────────────────────────────────

def load_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def effective_kills(kills: dict[str, dict]) -> dict[str, dict]:
    """Kills that actually landed inside the ReAct loop.

    A kill can arrive after mini has finished its loop and released its records
    (the loop is the small part of a ver99 run; git-apply and the CI wait are
    the rest). The replacement worker then has nothing to resume from and
    correctly starts over — cheap, because the LLM calls replay from cache. That
    is not a resume failure, and scoring it as one would make the arm's verdict
    depend on where the dice landed. `record_after_kill` measures it directly:
    the record was re-checked a moment after the SIGKILL.

    Older event files have no such field; those kills are counted as effective,
    which is the conservative reading (it can only produce a FAIL to look at).
    """
    return {k: e for k, e in kills.items() if e.get("record_after_kill", "present") != "gone"}


def index_events(events: list[dict]) -> dict:
    """Split the JSONL stream into the views the assertions need."""
    return {
        "kills": {e["run_key"]: e for e in events if e["kind"] == "kill"},
        "envs": {e["run_key"]: e for e in events if e["kind"] == "env"},
        "observed": {e["run_key"]: e for e in events if e["kind"] == "observe"},
        "purged": {e["run_key"] for e in events if e["kind"] == "purge"},
        "disk_peak": max([e.get("bytes") or 0 for e in events if e["kind"] == "disk"] or [0]),
        "container_peak": max([e.get("containers") or 0 for e in events
                               if e["kind"] == "disk"] or [0]),
    }


def journal_records(journal_dir: Path, start: float, end: float) -> dict[str, dict]:
    """instance_id -> newest RunRecord written inside this arm's window."""
    out: dict[str, dict] = {}
    for p in glob.glob(str(journal_dir / "*" / "record.json")):
        mtime = os.path.getmtime(p)
        if not (start - 5 <= mtime <= end + 120):
            continue
        try:
            rec = json.load(open(p))
        except Exception:
            continue
        iid = rec.get("swebench_instance_id")
        if not iid:
            continue
        rec["_path"], rec["_mtime"] = p, mtime
        if iid not in out or mtime > out[iid]["_mtime"]:
            out[iid] = rec
    return out


def parse_attach(lines) -> dict[str, list[str]]:
    """bug_id -> [container ids it attached to], from the orchestrator log.

    Takes any iterable of lines (an open file, a list) — or a whole string,
    which is split for you. Prefer `parse_attach_file`.
    """
    if isinstance(lines, str):
        lines = lines.splitlines()
    attach: dict[str, list[str]] = {}
    for line in lines:
        m = _ATTACH.search(line)
        if m:
            attach.setdefault(m.group(2), []).append(m.group(1))
    return attach


def parse_attach_file(path: Path) -> dict[str, list[str]]:
    """Stream the log. Never load it.

    The orchestrator inherits every worker's stdout, so after a 100-instance
    L3 run this file is **12 GB**. `read_text()` on it allocated the whole
    thing, got the verifier OOM-killed (rc=137, the box has no swap), and
    pulled 12 GB off a cold page cache on the way — which is what actually took
    the host's sshd down, three times, while the GitLab fetches it was blamed
    on had already completed. Streaming keeps memory flat and the read
    sequential.
    """
    if not path.exists():
        return {}
    with path.open(errors="ignore") as fh:
        return parse_attach(fh)


# ── assertions ───────────────────────────────────────────────────────────────

def subset_bias(judgeable: dict[str, dict], everything: dict[str, dict]) -> str:
    """One line describing how the judgeable subset differs from the population.

    An instance drops out of the subset because one of its cached responses
    missed, and a long trajectory has more chances to miss — so the judgeable
    set skews SHORT, while resume bugs plausibly skew long. Printing the two
    call-count distributions side by side keeps that caveat attached to the
    verdict instead of living in someone's memory.
    """
    def calls(d):
        return sorted(r.get("llm_call_count") or 0 for r in d.values())

    def fmt(xs):
        if not xs:
            return "n=0"
        mid = xs[len(xs) // 2]
        return f"n={len(xs)} median={mid} max={max(xs)}"

    return f"judgeable {fmt(calls(judgeable))}  |  all {fmt(calls(everything))}"


def compare_arms(ctrl: dict[str, dict], chaos: dict[str, dict],
                 killed_iids: set[str], patch_of=None) -> dict:
    """Per-instance control-vs-chaos comparison.

    `patch_of(record) -> str | None` supplies the produced patch. A pair where
    either side is None/empty is counted as *unavailable*, never as identical:
    two empty strings comparing equal is exactly how this check went vacuous
    the first time.
    """
    patch_of = patch_of or (lambda rec: None)
    rows, patch_bad, meta_bad, no_resume, patch_na = [], [], [], [], []
    for iid in sorted(set(ctrl) | set(chaos)):
        a, b = ctrl.get(iid), chaos.get(iid)
        was_killed = iid in killed_iids
        if a is None or b is None:
            meta_bad.append((iid, f"record present in ctrl={a is not None} chaos={b is not None}"))
            rows.append({"instance": iid, "killed": was_killed, "patch": "MISSING"})
            continue
        pa, pb = patch_of(a), patch_of(b)
        if not pa or not pb:
            patch_na.append((iid, f"ctrl={'yes' if pa else 'no'} chaos={'yes' if pb else 'no'}"))
            same = None
        else:
            same = pa == pb
        if same is False:
            patch_bad.append(iid)
        if a.get("resolved") != b.get("resolved") or a.get("outcome") != b.get("outcome"):
            meta_bad.append((iid, f"outcome {a.get('outcome')}->{b.get('outcome')}, "
                                  f"resolved {a.get('resolved')}->{b.get('resolved')}"))
        if was_killed and not b.get("step_resume_count"):
            no_resume.append(iid)
        rows.append({
            "instance": iid, "killed": was_killed,
            "patch": {True: "SAME", False: "DIFFER", None: "N/A"}[same],
            "resolved": f"{a.get('resolved')}/{b.get('resolved')}",
            "calls": f"{a.get('llm_call_count')}/{b.get('llm_call_count')}",
            "resume": b.get("step_resume_count"),
            "from": b.get("step_resumed_from_step"),
            "replay": b.get("step_replayed_command_count"),
        })
    return {"rows": rows, "patch_bad": patch_bad, "meta_bad": meta_bad,
            "no_resume": no_resume, "patch_na": patch_na}


def attach_mismatches(kills: dict[str, dict], attach: dict[str, list[str]]) -> list[tuple]:
    """A resumed run must re-attach to the container it was killed on."""
    bad = []
    for key, k in kills.items():
        got = attach.get(key, [])
        if not got:
            bad.append((key, k.get("instance"), "no 'resume: attached' line"))
        elif any(c != k.get("container_id") for c in got):
            bad.append((key, k.get("instance"),
                        f"attached {got} != killed-on {k.get('container_id')}"))
    return bad


def container_crosstalk(attach: dict[str, list[str]]) -> list[tuple]:
    """No container id may ever be attached under two different run keys."""
    owner: dict[str, str] = {}
    bad = []
    for key, cids in attach.items():
        for c in cids:
            if owner.setdefault(c, key) != key:
                bad.append((c, owner[c], key))
    return bad


def isolation_breaks(envs: dict[str, dict], records: list[dict]) -> list[tuple]:
    """The image checkpointed under a run_key must name that run's own instance."""
    by_bug = {r.get("bug_id"): r for r in records if r.get("bug_id")}
    bad = []
    for key, e in envs.items():
        rec = by_bug.get(key)
        if rec and e.get("instance") and rec.get("swebench_instance_id") != e["instance"]:
            bad.append((key, e["instance"], rec.get("swebench_instance_id")))
    return bad


def build_checks(cmp_result: dict, mismatch: list, crosstalk: list, iso_bad: list,
                 unpurged: list, leftover_dirs: list, leftover_containers: str) -> list[tuple]:
    def v(bad):
        return OK if not bad else BAD

    na = cmp_result.get("patch_na") or []
    if cmp_result["patch_bad"]:
        patch_verdict, patch_detail = BAD, cmp_result["patch_bad"]
    elif na:
        # Not "everything matched" — "we could not tell". Reported separately so
        # a missing token or an errored run cannot masquerade as a green run.
        patch_verdict, patch_detail = SKIP, na
    else:
        patch_verdict, patch_detail = OK, []
    return [
        ("patch identical to the control arm", patch_verdict, patch_detail),
        ("outcome/resolved identical", v(cmp_result["meta_bad"]), cmp_result["meta_bad"]),
        ("every killed run resumed (step_resume_count>=1)",
         v(cmp_result["no_resume"]), cmp_result["no_resume"]),
        ("re-attached to its OWN container", v(mismatch), mismatch),
        ("no container attached under two run keys", v(crosstalk), crosstalk),
        ("checkpoint records isolated per run", v(iso_bad), iso_bad),
        ("all observed runs purged", v(unpurged), unpurged),
        ("checkpoint dir empty at end", v(leftover_dirs), leftover_dirs),
        ("no leaked mini containers", v(leftover_containers), leftover_containers),
    ]


# ── report ───────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--control", required=True, help="control arm name")
    ap.add_argument("--chaos", required=True, help="chaos arm name")
    ap.add_argument("--w2-dir", default="~/.sdlcma/w2")
    ap.add_argument("--journal-dir", default="evaluation/journal")
    ap.add_argument("--orch-log", default="")
    ap.add_argument("--checkpoint-dir",
                    default=os.environ.get("BF_STEP_CHECKPOINT_DIR", "~/.sdlcma/step_checkpoints"))
    ap.add_argument("--clean-subset", default="",
                    help="file of instances that replayed 100%% in every arm "
                         "(cache_hitrate.py --clean-subset); the sharp checks are "
                         "restricted to these, since a cache miss forks the trajectory")
    ap.add_argument("--gitlab-api", default="",
                    help="GitLab API base for fetching the produced patch "
                         "(default $GITLAB_API; token from $GITLAB_PRIVATE_TOKEN)")
    ap.add_argument("--fetch-delay", type=float, default=0.0,
                    help="seconds to pace consecutive GitLab compare calls. One "
                         "call per instance per arm is ~2N requests, each of "
                         "which makes gitaly diff two refs of a large repo; "
                         "issued back-to-back against a cold page cache that "
                         "saturates the disk and can take the host's sshd with "
                         "it. 0 (default) keeps the old behaviour.")
    ap.add_argument("--diff-cache", default="",
                    help="file the fetched patches are read from and written back "
                         "to, incrementally. GitLab is often reachable only from "
                         "the host that ran the sweep, and that host is the one "
                         "under load; with this, the fetch survives its death and "
                         "the verdicts can be finished anywhere the journal is.")
    args = ap.parse_args()

    w2 = Path(os.path.expanduser(args.w2_dir))
    journal = Path(os.path.expanduser(args.journal_dir))
    orch_log = Path(os.path.expanduser(args.orch_log or (w2 / "logs" / "orch.log")))
    cp_dir = Path(os.path.expanduser(args.checkpoint_dir))

    ctrl_arm = json.loads((w2 / "arms" / f"{args.control}.json").read_text())
    chaos_arm = json.loads((w2 / "arms" / f"{args.chaos}.json").read_text())
    ctrl_all = journal_records(journal, ctrl_arm["start"], ctrl_arm["end"])
    chaos_all = journal_records(journal, chaos_arm["start"], chaos_arm["end"])
    ev = index_events(load_events(w2 / f"events_{args.chaos}.jsonl"))

    # A cache miss forks that instance's trajectory, so it is evidence for
    # nothing. The sharp checks run on the judgeable subset; the full population
    # still gets its resolved counts printed, as the soft (noisy) number.
    ctrl, chaos = ctrl_all, chaos_all
    subset = None
    if args.clean_subset:
        subset = {l.strip() for l in Path(os.path.expanduser(args.clean_subset))
                  .read_text().splitlines() if l.strip()}
        ctrl = {i: r for i, r in ctrl_all.items() if i in subset}
        chaos = {i: r for i, r in chaos_all.items() if i in subset}

    api = args.gitlab_api or os.environ.get("GITLAB_API", "")
    token = os.environ.get("GITLAB_PRIVATE_TOKEN", "")
    cache_path = Path(os.path.expanduser(args.diff_cache)) if args.diff_cache else None
    diff_cache = load_diff_cache(cache_path)

    patch_of = make_patch_fetcher(api, token, diff_cache, cache_path,
                                  delay=args.fetch_delay)

    eff = effective_kills(ev["kills"])
    missed = {k: e for k, e in ev["kills"].items() if k not in eff}
    killed_iids = {k.get("instance") for k in eff.values() if k.get("instance")}
    cmp_result = compare_arms(ctrl, chaos, killed_iids, patch_of=patch_of)
    attach = parse_attach_file(orch_log)
    mismatch = attach_mismatches(eff, attach)
    crosstalk = container_crosstalk(attach)
    iso_bad = isolation_breaks(ev["envs"], list(ctrl.values()) + list(chaos.values()))
    unpurged = sorted(set(ev["observed"]) - ev["purged"])
    leftover_dirs = sorted(p.name for p in cp_dir.iterdir() if p.is_dir()) if cp_dir.exists() else []
    leftover_containers = subprocess.run(
        ["docker", "ps", "--filter", "name=minisweagent", "--format", "{{.ID}} {{.Status}}"],
        capture_output=True, text=True).stdout.strip()

    print(f"control arm {args.control}: {len(ctrl)} journal records "
          f"(of {len(ctrl_all)} in the arm)")
    print(f"chaos   arm {args.chaos}: {len(chaos)} journal records "
          f"(of {len(chaos_all)} in the arm)")
    if subset is not None:
        print(f"judgeable subset: {len(ctrl)} instances — sharp checks below apply "
              f"to these only")
        print(f"  call-count bias: {subset_bias(ctrl, ctrl_all)}")
        for arm_name, recs in ((args.control, ctrl_all), (args.chaos, chaos_all)):
            res = sum(1 for r in recs.values() if r.get("resolved"))
            print(f"  soft (whole population, noisy) {arm_name}: resolved {res}/{len(recs)}")
    print(f"observed runs={len(ev['observed'])} killed={len(ev['kills'])} "
          f"(effective={len(eff)}, missed the loop={len(missed)}) purged={len(ev['purged'])}")
    for key, e in missed.items():
        print(f"  · kill on {e.get('instance')} landed after the loop had finished "
              f"(step={e.get('step')}, records already released) — resume not exercised")
    print(f"checkpoint dir peak = {ev['disk_peak']/1e6:.1f} MB   "
          f"peak mini containers = {ev['container_peak']}")
    print()
    hdr = (f"{'instance':40} {'killed':6} {'patch':8} {'resolved':14} "
           f"{'calls c/k':11} {'resume':6} {'from':5} {'replay':6}")
    print(hdr)
    print("-" * len(hdr))
    for r in cmp_result["rows"]:
        print(f"{r['instance']:40} {str(r['killed']):6} {r['patch']:8} "
              f"{str(r.get('resolved')):14} {str(r.get('calls')):11} "
              f"{str(r.get('resume')):6} {str(r.get('from')):5} {str(r.get('replay')):6}")

    print()
    print("=" * 78)
    checks = build_checks(cmp_result, mismatch, crosstalk, iso_bad, unpurged,
                          leftover_dirs, leftover_containers)
    for name, verdict, detail in checks:
        print(f"[{verdict}] {name}")
        if verdict in (BAD, SKIP) and detail:
            print(f"        {detail}")
    print("=" * 78)
    saved = sum((k.get("step") or 0) for k in ev["kills"].values())
    replayed = sum((chaos[i].get("step_replayed_command_count") or 0) for i in chaos)
    print(f"LLM steps saved by resume (completed steps at kill time): {saved}")
    print(f"sum step_replayed_command_count (§7.2's previously unmeasurable quantity): {replayed}")
    print(f"generated {time.strftime('%Y-%m-%d %H:%M:%S')}")
    raise SystemExit(1 if any(v == BAD for _, v, _ in checks) else 0)


if __name__ == "__main__":
    main()
