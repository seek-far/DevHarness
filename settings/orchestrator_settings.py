"""
OrchestratorSettings: two-step configuration loading.

Step 1 — EnvProbe: reads only the ENV variable (or root .env), gets the current environment name.
Step 2 — OrchestratorSettings: loads the corresponding orchestrator_<env>.env by environment name,
          then stacks environment variables on top.

Priority (high → low):
  environment variables > orchestrator_<env>.env > code defaults

Usage:
  from settings.orchestrator_settings import cfg
  cfg.redis_url
"""
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict
from .base_settings import BaseAppSettings

BASE_DIR = Path(__file__).resolve().parent  # …/settings/


# ── Step 1: probe, read ENV only ─────────────────────────────

class _EnvProbe(BaseSettings):
    """Only used to probe the ENV field; not exposed externally."""
    env: str = "local"
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",   # shared .env (may not exist)
        extra="ignore",
    )


_probe = _EnvProbe()


# ── Step 2: full configuration ────────────────────────────────

class OrchestratorSettings(BaseAppSettings):
    """
    Orchestrator-specific configuration.
    Valid env values and corresponding files:
      local               → orchestrator_local.env
      local_multi_process → orchestrator_local_multi_process.env
      test                → orchestrator_test.env
      production          → orchestrator_production.env
    """
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / f"orchestrator_{_probe.env}.env",
        env_file_encoding="utf-8",
        extra="ignore",          # ignore undefined fields in .env
    )


# Module-level singleton — entire process shares the same configuration
cfg: OrchestratorSettings = OrchestratorSettings()
