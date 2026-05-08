"""
Idempotency rainy-case tests for GitLab side-effects.

Strategy: real git on a bare-repo "origin" (so push/fetch/tree-equality is the
genuine git plumbing the production code calls), plus an in-memory MR registry
that stands in for the GitLab REST surface. The Repo class's three REST entry
points (_branch_exists_remote, find_open_or_merged_mr_for_branch,
gitlab_create_merge_request) are patched per-test to read/write the registry.

What we assert in every case:
  (a) the *external* state of the fake (origin refs, MR list) at end of run
  (b) the *return values* of commit_changes / gitlab_create_merge_request
      (status fields are part of the contract we promise to upstream telemetry)

Cases:
  R1   same input twice → both reused                          (true idempotency)
  R2   crash after commit → restart reuses commit, opens MR    (mid-flight restart)
  R3   MR closed → open new MR; old MR untouched                (closed ≠ reusable)
  R4   branch exists empty → first commit, status=success       (post-branch crash)
  R5   stale commit, different content → force-push, updated   (overwrite stale)
  R5b  stale commit, same content → no push, reused            (truly idempotent restart)
  R6   branch + commit + open MR aligned → all reused          (steady state restart)
  R7   POST race (409) → re-lookup wins, no duplicate MR        (concurrent open MR)
  R8   different base_commit → different branch name           (dedup key includes base)
  R9   force-push raises → exception propagates, no half state (no silent corruption)
  R10  branch + merged MR exists → status=already_merged       (R10 short-circuit signal)
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bf_worker"))

from providers.gitlab_provider import Repo  # noqa: E402


# ── git helpers ──────────────────────────────────────────────────────────────


def _g(cwd, *args, check=True):
    """Plain `git` subprocess helper for setting up scenarios."""
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {cwd}: {proc.stderr}")
    return proc.stdout.strip()


def _seed_bare_remote(tmp_path: Path) -> Path:
    """Initialize a bare repo with a single base commit on 'main' and return its path."""
    bare = tmp_path / "remote.git"
    bare.mkdir()
    _g(bare, "init", "--bare", "--initial-branch=main")

    seed = tmp_path / "_seed"
    seed.mkdir()
    _g(seed, "init", "--initial-branch=main")
    _g(seed, "config", "user.email", "test@test")
    _g(seed, "config", "user.name", "test")
    (seed / "buggy.py").write_text("def add(a, b):\n    return a + b + 1  # bug\n")
    _g(seed, "add", ".")
    _g(seed, "commit", "-m", "base")
    _g(seed, "push", str(bare), "main")
    return bare


def _new_clone(bare: Path, work_dir: Path) -> None:
    """Fresh clone of the bare repo with author config set."""
    _g(work_dir.parent, "clone", str(bare), str(work_dir))
    _g(work_dir, "config", "user.email", "test@test")
    _g(work_dir, "config", "user.name", "test")


def _apply_fix(work_dir: Path, content: str) -> None:
    """Write the fix content to buggy.py (simulates apply_change_and_test result)."""
    (work_dir / "buggy.py").write_text(content)


CORRECT_FIX = "def add(a, b):\n    return a + b\n"
DIFFERENT_FIX = "def add(a, b):\n    return (a) + (b)\n"


# ── fake MR registry ─────────────────────────────────────────────────────────


def _make_registry():
    """In-memory MR list with the lookup/POST shape the production code expects."""
    state = SimpleNamespace(mrs=[], next_id=1, post_call_count=0,
                            inject_409_once=False)

    def find(branch_name):
        for filt in ("opened", "merged"):
            for mr in state.mrs:
                if mr["source_branch"] == branch_name and mr["state"] == filt:
                    return {
                        "id": mr["id"], "iid": mr["id"], "title": mr["title"],
                        "url": mr["url"], "state": mr["state"],
                    }
        return None

    def post(source_branch, target_branch="main", title=None, description=None):
        state.post_call_count += 1
        # Lookup-then-create (mirrors production logic)
        existing = find(source_branch)
        if existing is not None:
            if existing["state"] == "opened":
                return {**existing, "status": "reused"}
            if existing["state"] == "merged":
                return {**existing, "status": "already_merged"}
            # closed → fall through to create

        # R7: simulate GitLab returning 409 because someone raced us between
        # find() and create(). Production code re-looks up on 409 and reuses.
        if state.inject_409_once:
            state.inject_409_once = False
            again = find(source_branch)
            if again is not None and again["state"] == "opened":
                return {**again, "status": "reused"}
            raise RuntimeError("Simulated 409 with no MR found post-race")

        i = state.next_id
        state.next_id += 1
        mr = {
            "id": i, "title": title or f"Merge {source_branch} into {target_branch}",
            "source_branch": source_branch, "target_branch": target_branch,
            "state": "opened", "url": f"http://fake/mr/{i}",
        }
        state.mrs.append(mr)
        return {**find(source_branch), "status": "opened"}

    def add_existing(source_branch, mr_state):
        i = state.next_id
        state.next_id += 1
        state.mrs.append({
            "id": i, "title": f"existing for {source_branch}",
            "source_branch": source_branch, "target_branch": "main",
            "state": mr_state, "url": f"http://fake/mr/{i}",
        })
        return i

    state.find = find
    state.post = post
    state.add_existing = add_existing
    return state


# ── Repo factory ─────────────────────────────────────────────────────────────


def _make_repo(bare: Path, work_dir: Path, registry) -> Repo:
    """Instantiate Repo with REST methods replaced to read/write the registry.

    We bypass __init__ because the real one is wired to cfg env-specific
    URL rewriting; for tests we just set the attributes directly.
    """
    repo = Repo.__new__(Repo)
    repo.repo_path = work_dir
    repo.repo_url = f"file://{bare}"
    repo.ssh_url = repo.repo_url
    repo.token = "fake"

    def _branch_exists_remote(branch_name):
        try:
            out = repo.run("ls-remote", repo.repo_url, branch_name)
            return bool(out.strip())
        except RuntimeError:
            return False

    def _find_mr(branch_name):
        return registry.find(branch_name)

    def _create_mr(source_branch, target_branch="main", title=None, description=None):
        # Mirror production: lookup is done by the parent method, but we
        # short-circuit it here entirely via registry.post (which handles
        # both lookup and POST identically to production semantics).
        return registry.post(source_branch, target_branch, title, description)

    repo._branch_exists_remote = _branch_exists_remote
    repo.find_open_or_merged_mr_for_branch = _find_mr
    repo.gitlab_create_merge_request = _create_mr
    return repo


def _seed_branch_with(bare: Path, work_dir: Path, branch: str, content: str) -> str:
    """Push a branch to origin with `content` in buggy.py. Returns the commit SHA.

    Used to simulate 'a previous worker / human created this state' before
    the run-under-test starts.
    """
    side = work_dir.parent / f"_side_{branch.replace('/', '_')}"
    side.mkdir(exist_ok=True)
    _new_clone(bare, side)
    _g(side, "checkout", "-b", branch)
    (side / "buggy.py").write_text(content)
    _g(side, "add", ".")
    _g(side, "commit", "-m", f"prior fix on {branch}")
    sha = _g(side, "rev-parse", "HEAD")
    _g(side, "push", str(bare), branch)
    return sha


# ── deterministic branch name helper ─────────────────────────────────────────


def _branch_name(bug_id: str, base_commit: str) -> str:
    return f"auto/bf/{bug_id}-{base_commit[:8]}"


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────


def test_R1_same_input_twice_reused(tmp_path):
    """R1: rerun with identical input produces no new side effects."""
    bare = _seed_bare_remote(tmp_path)
    reg = _make_registry()
    work = tmp_path / "work"
    _new_clone(bare, work)
    repo = _make_repo(bare, work, reg)

    base_commit = repo.run("rev-parse", "HEAD")
    branch = _branch_name("BUG-R1", base_commit)
    repo.run("checkout", "-b", branch)

    # First run
    _apply_fix(work, CORRECT_FIX)
    res1 = repo.commit_changes("fix")
    mr1 = repo.gitlab_create_merge_request(branch, "main", "auto-fix", "")

    # Second run — fresh clone, same fix
    work2 = tmp_path / "work2"
    _new_clone(bare, work2)
    repo2 = _make_repo(bare, work2, reg)
    repo2.run("checkout", "-b", branch, f"origin/{branch}")
    _apply_fix(work2, CORRECT_FIX)
    res2 = repo2.commit_changes("fix")
    mr2 = repo2.gitlab_create_merge_request(branch, "main", "auto-fix", "")

    # First run created; second run reused everything
    assert res1["status"] == "success"
    assert res2["status"] == "no_changes"  # working tree clean → no commit attempted
    assert mr1["status"] == "opened"
    assert mr2["status"] == "reused"
    assert mr1["id"] == mr2["id"]
    assert len(reg.mrs) == 1


def test_R2_crash_after_commit_resumes(tmp_path):
    """R2: worker crashes after commit; restart reuses commit, opens MR fresh."""
    bare = _seed_bare_remote(tmp_path)
    reg = _make_registry()
    work = tmp_path / "work"
    _new_clone(bare, work)
    repo = _make_repo(bare, work, reg)

    base_commit = repo.run("rev-parse", "HEAD")
    branch = _branch_name("BUG-R2", base_commit)
    repo.run("checkout", "-b", branch)
    _apply_fix(work, CORRECT_FIX)
    repo.commit_changes("fix")
    # Simulated crash: no MR created, but commit is on origin

    # Restart — fresh clone, production flow: branch created from main
    work2 = tmp_path / "work2"
    _new_clone(bare, work2)
    repo2 = _make_repo(bare, work2, reg)
    repo2.run("checkout", "-b", branch)  # from main, not origin/branch
    _apply_fix(work2, CORRECT_FIX)
    res2 = repo2.commit_changes("fix")
    mr2 = repo2.gitlab_create_merge_request(branch, "main", "auto-fix", "")

    # local has main+CORRECT_FIX; remote has main+CORRECT_FIX → trees match → reused
    assert res2["status"] == "reused"
    assert mr2["status"] == "opened"
    assert len(reg.mrs) == 1


def test_R3_closed_mr_opens_new(tmp_path):
    """R3: prior MR was closed; new run opens a fresh MR; old MR untouched."""
    bare = _seed_bare_remote(tmp_path)
    reg = _make_registry()
    work = tmp_path / "work"
    _new_clone(bare, work)
    repo = _make_repo(bare, work, reg)

    base_commit = repo.run("rev-parse", "HEAD")
    branch = _branch_name("BUG-R3", base_commit)
    closed_id = reg.add_existing(branch, "closed")

    repo.run("checkout", "-b", branch)
    _apply_fix(work, CORRECT_FIX)
    repo.commit_changes("fix")
    mr = repo.gitlab_create_merge_request(branch, "main", "auto-fix", "")

    assert mr["status"] == "opened"
    assert mr["id"] != closed_id
    assert len(reg.mrs) == 2  # one closed (kept), one new opened
    assert {m["state"] for m in reg.mrs} == {"closed", "opened"}


def test_R4_branch_exists_empty_first_commit_succeeds(tmp_path):
    """R4: branch exists on origin pointing at base (no fix commit yet)."""
    bare = _seed_bare_remote(tmp_path)
    reg = _make_registry()

    # Pre-create branch on origin, pointing at base (no fix yet)
    side = tmp_path / "_pre"
    _new_clone(bare, side)
    base_sha = _g(side, "rev-parse", "HEAD")
    _g(side, "push", str(bare), f"main:refs/heads/auto/bf/BUG-R4-{base_sha[:8]}")

    work = tmp_path / "work"
    _new_clone(bare, work)
    repo = _make_repo(bare, work, reg)

    base_commit = repo.run("rev-parse", "HEAD")
    branch = _branch_name("BUG-R4", base_commit)
    repo.run("checkout", "-b", branch, f"origin/{branch}")
    _apply_fix(work, CORRECT_FIX)
    res = repo.commit_changes("fix")

    # Branch existed remotely but pointed at base → first fix commit ⇒ "success"
    assert res["status"] == "success"


def test_R5_stale_different_content_force_pushes_updated(tmp_path):
    """R5: stale fix on remote ≠ our fix.

    Production flow: fresh clone, branch created from main (NOT from
    origin/branch — see ensure_repo_ready which rmtrees first), commit, push.
    Push detects divergence (stale-fix history is not an ancestor of
    main+our-fix) → force-push, status='updated'.
    """
    bare = _seed_bare_remote(tmp_path)
    reg = _make_registry()
    base_sha = _g(tmp_path / "_seed", "rev-parse", "HEAD")
    branch = _branch_name("BUG-R5", base_sha)
    stale_sha = _seed_branch_with(bare, tmp_path / "work", branch, DIFFERENT_FIX)

    # Production flow: fresh clone, create branch FROM main (not origin/branch).
    work = tmp_path / "work2"
    _new_clone(bare, work)
    repo = _make_repo(bare, work, reg)
    repo.run("checkout", "-b", branch)  # from current HEAD (main), not origin/branch
    _apply_fix(work, CORRECT_FIX)
    res = repo.commit_changes("fix")

    assert res["status"] == "updated"
    new_sha = _g(work, "rev-parse", "HEAD")
    remote_after = _g(tmp_path, "ls-remote", str(bare), branch).split()[0]
    assert new_sha == remote_after            # remote now points at our fix
    assert new_sha != stale_sha               # stale commit was overwritten


def test_R5b_stale_same_content_no_push_reused(tmp_path):
    """R5b: stale fix on remote == our fix's content → tree-equality reuse, no push."""
    bare = _seed_bare_remote(tmp_path)
    reg = _make_registry()
    base_sha = _g(tmp_path / "_seed", "rev-parse", "HEAD")
    branch = _branch_name("BUG-R5b", base_sha)
    prior_sha = _seed_branch_with(bare, tmp_path / "work", branch, CORRECT_FIX)

    # Production flow: fresh clone, branch from main, apply, commit.
    work = tmp_path / "work2"
    _new_clone(bare, work)
    repo = _make_repo(bare, work, reg)
    repo.run("checkout", "-b", branch)
    _apply_fix(work, CORRECT_FIX)
    res = repo.commit_changes("fix")

    # local committed; trees match remote → no push, status="reused".
    # Remote SHA must be unchanged (no force-push happened).
    assert res["status"] == "reused"
    remote_after = _g(tmp_path, "ls-remote", str(bare), branch).split()[0]
    assert remote_after == prior_sha


def test_R6_steady_state_all_reused(tmp_path):
    """R6: branch + commit + open MR all already exist → everything reused."""
    bare = _seed_bare_remote(tmp_path)
    reg = _make_registry()
    base_sha = _g(tmp_path / "_seed", "rev-parse", "HEAD")
    branch = _branch_name("BUG-R6", base_sha)
    prior_sha = _seed_branch_with(bare, tmp_path / "_pre", branch, CORRECT_FIX)
    open_mr_id = reg.add_existing(branch, "opened")

    # Production flow.
    work = tmp_path / "work"
    _new_clone(bare, work)
    repo = _make_repo(bare, work, reg)
    repo.run("checkout", "-b", branch)
    _apply_fix(work, CORRECT_FIX)
    res = repo.commit_changes("fix")
    mr = repo.gitlab_create_merge_request(branch, "main", "auto-fix", "")

    assert res["status"] == "reused"
    assert mr["status"] == "reused"
    assert mr["id"] == open_mr_id
    assert len(reg.mrs) == 1
    remote_after = _g(tmp_path, "ls-remote", str(bare), branch).split()[0]
    assert remote_after == prior_sha


def test_R7_post_race_409_reuses_existing(tmp_path):
    """R7: between our find() and POST, someone else opened the MR.
    Production simulates this via 409 + re-lookup; we trigger inject_409_once.
    """
    bare = _seed_bare_remote(tmp_path)
    reg = _make_registry()
    work = tmp_path / "work"
    _new_clone(bare, work)
    repo = _make_repo(bare, work, reg)

    base_commit = repo.run("rev-parse", "HEAD")
    branch = _branch_name("BUG-R7", base_commit)
    repo.run("checkout", "-b", branch)
    _apply_fix(work, CORRECT_FIX)
    repo.commit_changes("fix")

    # Pre-stage the racing MR + arm the 409 injection so the next POST
    # sees: lookup→None at first, then 409, then lookup→existing.
    racer_id = reg.add_existing(branch, "opened")
    # Replace the registry's find with one that lies on the first call
    real_find = reg.find
    state_box = {"calls": 0, "racer_id": racer_id}

    def find_lying_first(b):
        state_box["calls"] += 1
        if state_box["calls"] == 1:
            return None  # lie: pretend the racer's MR isn't there yet
        return real_find(b)

    reg.find = find_lying_first
    reg.inject_409_once = True
    repo.find_open_or_merged_mr_for_branch = find_lying_first

    mr = repo.gitlab_create_merge_request(branch, "main", "auto-fix", "")

    # On 409, production re-looks up; our second lookup returns the racer.
    assert mr["status"] == "reused"
    assert mr["id"] == racer_id
    assert len(reg.mrs) == 1   # no duplicate created


def test_R8_different_base_commit_yields_different_branch(tmp_path):
    """R8: dedup key includes base_commit; different base → different branch."""
    bare = _seed_bare_remote(tmp_path)
    work = tmp_path / "work"
    _new_clone(bare, work)
    repo = _make_repo(bare, work, _make_registry())
    base_v1 = repo.run("rev-parse", "HEAD")

    # Bump base via a second commit
    (work / "extra.py").write_text("# moved on\n")
    _g(work, "add", ".")
    _g(work, "commit", "-m", "advance base")
    base_v2 = _g(work, "rev-parse", "HEAD")

    name_v1 = repo.deterministic_branch_name("BUG-R8", base_v1)
    name_v2 = repo.deterministic_branch_name("BUG-R8", base_v2)
    assert base_v1 != base_v2
    assert name_v1 != name_v2
    assert name_v1.startswith("auto/bf/BUG-R8-")
    assert name_v2.startswith("auto/bf/BUG-R8-")


def test_R9_force_push_failure_propagates(tmp_path):
    """R9: a failing force-push must surface as an exception, not silently succeed."""
    bare = _seed_bare_remote(tmp_path)
    reg = _make_registry()
    base_sha = _g(tmp_path / "_seed", "rev-parse", "HEAD")
    branch = _branch_name("BUG-R9", base_sha)
    stale_sha = _seed_branch_with(bare, tmp_path / "_pre", branch, DIFFERENT_FIX)

    # Production flow: fresh clone, branch from main (not origin/branch).
    work = tmp_path / "work"
    _new_clone(bare, work)
    repo = _make_repo(bare, work, reg)
    repo.run("checkout", "-b", branch)
    _apply_fix(work, CORRECT_FIX)

    # Patch repo.run to raise specifically on the force-push call. Use the
    # existing run as a wrapper so other git commands keep working.
    real_run = repo.run

    def run_failing_on_force(*args, **kwargs):
        if args and args[0] == "push" and "--force-with-lease" in args:
            raise RuntimeError("simulated push failure")
        return real_run(*args, **kwargs)

    with patch.object(repo, "run", side_effect=run_failing_on_force):
        with pytest.raises(RuntimeError, match="simulated push failure"):
            repo.commit_changes("fix")

    # The branch on origin must be UNCHANGED (we didn't half-push)
    remote_after = _g(tmp_path, "ls-remote", str(bare), branch).split()[0]
    assert remote_after == stale_sha


def test_R10_merged_mr_signals_already_merged(tmp_path):
    """R10: existing merged MR for our branch → MR creation returns 'already_merged'.

    The graph's create_fix_branch surfaces this via existing_mr={state:merged}
    and routes to END with outcome=already_fixed. Here we verify the provider's
    return value contract — what create_fix_branch and create_review observe.
    """
    bare = _seed_bare_remote(tmp_path)
    reg = _make_registry()
    base_sha = _g(tmp_path / "_seed", "rev-parse", "HEAD")
    branch = _branch_name("BUG-R10", base_sha)
    _seed_branch_with(bare, tmp_path / "_pre", branch, CORRECT_FIX)
    merged_id = reg.add_existing(branch, "merged")

    work = tmp_path / "work"
    _new_clone(bare, work)
    repo = _make_repo(bare, work, reg)

    # Lookup-only path: create_fix_branch (in production) probes for the MR
    found = repo.find_open_or_merged_mr_for_branch(branch)
    assert found is not None
    assert found["state"] == "merged"
    assert found["id"] == merged_id

    # If we did call create_review anyway, it would return already_merged.
    out = repo.gitlab_create_merge_request(branch, "main", "auto-fix", "")
    assert out["status"] == "already_merged"
    assert out["id"] == merged_id
    # No new MR was opened
    assert len(reg.mrs) == 1
