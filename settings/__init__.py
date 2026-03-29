"""
settings package public interface.

Quick imports:
  from settings import orchestrator_cfg   # Orchestrator singleton
  from settings import worker_cfg         # Worker singleton
  from settings import OrchestratorSettings, WorkerSettings  # types
"""
from .orchestrator_settings import OrchestratorSettings, cfg as orchestrator_cfg
from .worker_settings import WorkerSettings, cfg as worker_cfg

__all__ = [
    "OrchestratorSettings",
    "WorkerSettings",
    "orchestrator_cfg",
    "worker_cfg",
]
