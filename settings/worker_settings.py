"""
WorkerSettings: worker process-specific configuration, loading logic is the same
as OrchestratorSettings but reads worker_<env>.env files.

Usage:
  from settings.worker_settings import cfg
  cfg.redis_url
"""
import tempfile
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict
from .base_settings import BaseAppSettings

BASE_DIR = Path(__file__).resolve().parent


class _EnvProbe(BaseSettings):
    env: str = "local_multi_process"
    #model_config = SettingsConfigDict(
    #    env_file=BASE_DIR / ".env",
    #    extra="ignore",
    #)


_probe = _EnvProbe()


class WorkerSettings(BaseAppSettings):
    """
    Worker-specific configuration.
    Valid env values and corresponding files:
      local               → worker_local.env
      local_multi_process → worker_local_multi_process.env
      test                → worker_test.env
      production          → worker_production.env
    """
    gitlab_ssh_port: str = "2222"
    gitlab_username: str
    # Base directory for cloning repos; each worker appends its bug_id.
    # Resolves to /tmp/dh_repo (Linux) or %TEMP%\dh_repo (Windows).
    repo_base_path: str = str(Path(tempfile.gettempdir()) / "dh_repo")
    # Self-hosted backends (vLLM, llama.cpp, Ollama, …) don't validate the
    # key but the OpenAI client refuses an empty string — "EMPTY" is the
    # vLLM-community convention. Cloud backends override via env file.
    llm_api_key: str = "EMPTY"
    # Per-LLM-call HTTP timeout (seconds). Default 600s targets self-hosted
    # backends where a single Qwen2.5-Coder CoT step can run several
    # minutes; a hard ceiling matters because a degenerate-decoding loop
    # (e.g. token repetition near the context limit) can otherwise run
    # until the backend's own limit. Cloud backends never need this long
    # and can override down via env (e.g. LLM_REQUEST_TIMEOUT=60).
    llm_request_timeout: int = 600
    # When the worker's LLM endpoint is an SDLCMA llm_gateway (independent
    # FastAPI service that routes to one of N configured backends per the
    # request hint headers), set this flag in the env file so the worker
    # (a) attaches X-Sdlcma-Bug-Id / X-Sdlcma-Attempt to every LLM call and
    # (b) skips the startup /v1/models name check (the gateway exposes the
    # union of backend models; it cannot promise which backend a future
    # request will hit). Unset/false = legacy direct-to-backend path —
    # zero behaviour change.
    llm_via_gateway: bool = False
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / f"worker_{_probe.env}.env",
        env_file_encoding="utf-8",
        #extra="ignore",
        extra="allow",
    )


cfg: WorkerSettings = WorkerSettings()
