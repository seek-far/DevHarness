"""bf_worker/bf_worker.py must import cleanly when spawned the way the
orchestrator spawns it: launched as `python <abs-path>/bf_worker.py` from
an arbitrary cwd, with no `PYTHONPATH` propagated.

Regression test for a real incident — `sys.path.append(Path.cwd())` was
written AFTER the `from agent_config import ...` cascade that reaches
into `graph/routing.py` → `from settings import worker_cfg`. The append
happened too late; settings wasn't importable; the worker died at boot
with ModuleNotFoundError before it ever wrote a journal entry. Pin the
fix so re-ordering the imports can't silently bring this back.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[1]
_WORKER = _REPO_ROOT / "bf_worker" / "bf_worker.py"


def test_worker_imports_from_arbitrary_cwd_without_pythonpath(tmp_path):
    # Mimic orchestrator/spawner.py:_start_process — the worker is launched
    # as `python <abs>/bf_worker.py --bug-id ...` with `env=os.environ.copy()`
    # and no explicit `cwd=`. Subprocess inherits parent's cwd; PYTHONPATH
    # is whatever the parent had — typically NOT set.
    proc = subprocess.run(
        [sys.executable, str(_WORKER), "--help"],
        cwd=str(tmp_path),               # any cwd that ISN'T the repo root
        env={"PATH": "/usr/bin:/bin"},   # deliberately no PYTHONPATH
        capture_output=True,
        text=True,
        timeout=30,
    )
    # --help must complete cleanly even before any redis/network setup.
    # A ModuleNotFoundError boots before argparse runs → non-zero exit.
    assert proc.returncode == 0, (
        f"worker --help failed (rc={proc.returncode})\n"
        f"stderr:\n{proc.stderr[:2000]}\n"
        f"stdout:\n{proc.stdout[:2000]}"
    )
