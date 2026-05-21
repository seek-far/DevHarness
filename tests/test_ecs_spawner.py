"""EcsWorkerSpawner contract — boto3 RunTask shape and recovery semantics.

The actual AWS round-trip is rehearsed by the smoke script
(`infra/aws-ecs/gitlab-smoke.sh`); here we pin only what unit-testable code
guarantees:

* RunTask is called with launchType=EC2 + awsvpc network config built from
  comma-split subnets / security groups,
* the container override carries both ``command=["--bug-id", bug_id]`` AND
  the right environment shape (BUG_ID, REDIS_URL, project_*, ENV,
  BF_CHECKPOINT_BACKEND=none) — the bf-worker entrypoint is
  ``exec python bf_worker.py "$@"``, so omitting either silently breaks the
  worker invocation in a way that's hard to spot in CloudWatch,
* ENV passed to the worker is ``gitlab_saas`` (NOT "ecs") — there is no
  "ecs" branch in the GitLab provider; the cloud track reuses the
  established gitlab.com auth path,
* restart() bumps restart_count and terminates the prior task before
  starting a replacement.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# boto3 is the runtime dep that EcsWorkerSpawner.__init__ imports. We don't
# need a real client — patch it before constructing the spawner.
boto3_mock = MagicMock()
sys.modules.setdefault("boto3", boto3_mock)

from orchestrator.registry import WorkerRegistry  # noqa: E402
from orchestrator.spawner import EcsWorkerSpawner  # noqa: E402


def _make_spawner(ecs_client_mock=None, worker_network_mode="awsvpc"):
    """Construct EcsWorkerSpawner with the boto3 ECS client mocked out."""
    ecs_client = ecs_client_mock or MagicMock()
    with patch("boto3.client", return_value=ecs_client):
        spawner = EcsWorkerSpawner(
            registry=WorkerRegistry(),
            redis_url="redis://10.0.0.5:6379/0",
            cluster="sdlcma-cluster",
            task_def="bf-worker",
            subnets="subnet-aaa,subnet-bbb",
            security_groups="sg-xxx,sg-yyy",
            region="us-east-1",
            worker_env="gitlab_saas",
            worker_network_mode=worker_network_mode,
        )
    # In case boto3.client patching missed (different import order), force the
    # internal client to the mock so tests don't accidentally touch AWS.
    spawner._ecs = ecs_client
    return spawner, ecs_client


def _run_task_success(task_arn="arn:aws:ecs:us-east-1:111:task/sdlcma/abc123"):
    return {"tasks": [{"taskArn": task_arn}]}


# ── construction ────────────────────────────────────────────────────────────


def test_subnets_and_sgs_are_comma_split():
    s, _ = _make_spawner()
    assert s._subnets == ["subnet-aaa", "subnet-bbb"]
    assert s._security_groups == ["sg-xxx", "sg-yyy"]


def test_blank_entries_in_subnets_are_dropped():
    """' subnet-a , , subnet-b' → ['subnet-a','subnet-b']  (defensive)."""
    ecs = MagicMock()
    with patch("boto3.client", return_value=ecs):
        s = EcsWorkerSpawner(
            registry=WorkerRegistry(), redis_url="r",
            cluster="c", task_def="t",
            subnets=" subnet-a , , subnet-b ",
            security_groups="sg-x,, ,sg-y",
            region="us-east-1",
        )
    assert s._subnets == ["subnet-a", "subnet-b"]
    assert s._security_groups == ["sg-x", "sg-y"]


def test_default_network_mode_is_awsvpc():
    """The constructor default keeps existing callers byte-identical — the
    spawner ships network_mode=awsvpc by default; only OrchestratorSettings
    flips the live deploy default to "host" via ecs_worker_network_mode."""
    s, _ = _make_spawner()
    assert s._worker_network_mode == "awsvpc"


# ── spawn: RunTask call shape ───────────────────────────────────────────────


def test_spawn_calls_run_task_with_expected_overrides():
    spawner, ecs = _make_spawner()
    ecs.run_task.return_value = _run_task_success()

    entry = asyncio.run(spawner.spawn(
        bug_id="bug-1", project_id="42",
        project_web_url="https://gitlab.com/g/p", job_id="job-9",
    ))

    ecs.run_task.assert_called_once()
    kwargs = ecs.run_task.call_args.kwargs
    assert kwargs["cluster"] == "sdlcma-cluster"
    assert kwargs["taskDefinition"] == "bf-worker"
    assert kwargs["launchType"] == "EC2"
    netcfg = kwargs["networkConfiguration"]["awsvpcConfiguration"]
    assert netcfg["subnets"] == ["subnet-aaa", "subnet-bbb"]
    assert netcfg["securityGroups"] == ["sg-xxx", "sg-yyy"]
    # assignPublicIp is Fargate-only; passing it under launchType=EC2 returns
    # InvalidParameterException. The awsvpc ENI does NOT inherit
    # MapPublicIpOnLaunch for secondary ENIs anyway — outbound needs NAT/EIP.
    assert "assignPublicIp" not in netcfg

    override = kwargs["overrides"]["containerOverrides"][0]
    assert override["name"] == "bf-worker"
    # bug_id reaches the worker via command args (entrypoint = `python … "$@"`).
    assert override["command"] == ["--bug-id", "bug-1"]

    env = {e["name"]: e["value"] for e in override["environment"]}
    assert env["BUG_ID"] == "bug-1"
    assert env["REDIS_URL"] == "redis://10.0.0.5:6379/0"
    assert env["project_id"] == "42"
    assert env["project_web_url"] == "https://gitlab.com/g/p"
    assert env["job_id"] == "job-9"
    # Reuse the established gitlab.com auth path — NOT a new "ecs" worker env.
    assert env["ENV"] == "gitlab_saas"
    # Ephemeral task; persistent checkpoint would re-create the shared-state
    # contamination hazard (matches DockerWorkerSpawner).
    assert env["BF_CHECKPOINT_BACKEND"] == "none"

    assert entry.bug_id == "bug-1"
    assert entry.process.pid == "abc123"


def test_host_network_mode_omits_networkconfiguration():
    """ECS rejects `networkConfiguration` for bridge/host task definitions
    (InvalidParameterException). When the worker task def runs in host mode,
    the spawner must NOT include the key — only awsvpc tasks get it."""
    spawner, ecs = _make_spawner(worker_network_mode="host")
    ecs.run_task.return_value = _run_task_success()

    asyncio.run(spawner.spawn(bug_id="bug-h", project_id="1",
                              project_web_url="u", job_id="j"))

    kwargs = ecs.run_task.call_args.kwargs
    assert "networkConfiguration" not in kwargs
    # The container override (incl. command/env) is unchanged from awsvpc.
    assert kwargs["overrides"]["containerOverrides"][0]["command"] == ["--bug-id", "bug-h"]


def test_spawn_is_idempotent_when_bug_already_running():
    spawner, ecs = _make_spawner()
    ecs.run_task.return_value = _run_task_success()

    e1 = asyncio.run(spawner.spawn(bug_id="dup", project_id="1",
                                   project_web_url="u", job_id="j"))
    e2 = asyncio.run(spawner.spawn(bug_id="dup", project_id="1",
                                   project_web_url="u", job_id="j"))

    assert ecs.run_task.call_count == 1  # second call short-circuits
    assert e1 is e2


def test_spawn_raises_when_run_task_returns_no_tasks():
    """run_task can succeed-with-failures (returns {"failures": [...]}, empty
    tasks list). The spawner must raise rather than register a phantom entry."""
    spawner, ecs = _make_spawner()
    ecs.run_task.return_value = {"tasks": [], "failures": [{"reason": "RESOURCE:MEMORY"}]}

    with pytest.raises(RuntimeError, match="no tasks"):
        asyncio.run(spawner.spawn(bug_id="bug-x", project_id="1",
                                  project_web_url="u", job_id="j"))


# ── restart ─────────────────────────────────────────────────────────────────


def test_restart_increments_restart_count_and_stops_prior_task():
    spawner, ecs = _make_spawner()
    ecs.run_task.side_effect = [
        _run_task_success("arn:aws:ecs:us-east-1:111:task/sdlcma/first"),
        _run_task_success("arn:aws:ecs:us-east-1:111:task/sdlcma/second"),
    ]

    first = asyncio.run(spawner.spawn(bug_id="b", project_id="1",
                                      project_web_url="u", job_id="j"))
    assert first.restart_count == 0

    second = asyncio.run(spawner.restart(bug_id="b", project_id="1",
                                         project_web_url="u", job_id="j"))
    assert second.restart_count == 1
    # stop_task was called on the prior task ARN
    ecs.stop_task.assert_called_once()
    stop_kwargs = ecs.stop_task.call_args.kwargs
    assert "first" in stop_kwargs["task"]


# ── EcsTaskProxy: returncode reload from describe_tasks ─────────────────────


def test_proxy_reload_status_picks_up_exit_code_when_task_stopped():
    from orchestrator.spawner import EcsTaskProxy

    ecs = MagicMock()
    ecs.describe_tasks.return_value = {
        "tasks": [{
            "lastStatus": "STOPPED",
            "containers": [{"exitCode": 0}],
        }],
    }
    proxy = EcsTaskProxy(ecs, cluster="c", task_arn="arn:.../abc")
    proxy.reload_status()
    assert proxy.returncode == 0


def test_proxy_reload_status_stays_none_while_running():
    from orchestrator.spawner import EcsTaskProxy

    ecs = MagicMock()
    ecs.describe_tasks.return_value = {
        "tasks": [{"lastStatus": "RUNNING", "containers": []}],
    }
    proxy = EcsTaskProxy(ecs, cluster="c", task_arn="arn:.../abc")
    proxy.reload_status()
    assert proxy.returncode is None


def test_proxy_reload_status_keeps_none_on_empty_tasks_without_missing():
    """Freshly-spawned tasks have a brief window where describe-tasks returns
    empty `tasks` without a `failures` entry (the ARN exists per RunTask but
    isn't yet visible to describe-tasks). Treating that as exit(-1) caused the
    HealthMonitor to mark live workers as done seconds after spawn — verified
    in the AWS bug-fix run, the validation-event router then lost track of
    them. Only reason==MISSING is a real "task is gone" signal."""
    from orchestrator.spawner import EcsTaskProxy

    ecs = MagicMock()
    # Propagation window: ARN known to RunTask but not yet to describe-tasks.
    ecs.describe_tasks.return_value = {"tasks": [], "failures": []}
    proxy = EcsTaskProxy(ecs, cluster="c", task_arn="arn:.../abc")
    proxy.reload_status()
    assert proxy.returncode is None

    # Real "gone": describe-tasks reports MISSING.
    ecs.describe_tasks.return_value = {
        "tasks": [],
        "failures": [{"arn": "arn:.../abc", "reason": "MISSING"}],
    }
    proxy.reload_status()
    assert proxy.returncode == -1


def test_proxy_reload_status_swallows_transient_describe_errors():
    """Any one describe-tasks error must not unregister a live worker — same
    contamination problem as the empty-tasks race."""
    from orchestrator.spawner import EcsTaskProxy

    ecs = MagicMock()
    ecs.describe_tasks.side_effect = Exception("temporary network blip")
    proxy = EcsTaskProxy(ecs, cluster="c", task_arn="arn:.../abc")
    proxy.reload_status()
    assert proxy.returncode is None
