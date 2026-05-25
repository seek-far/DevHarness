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
    # LLM endpoint URL and model name. DECLARED HERE EXPLICITLY (rather
    # than relying on extra="allow" to absorb them from the env file)
    # because pydantic-settings v2 reverses env-var-vs-env-file priority
    # for undeclared "extra" fields: the env file wins over the env var,
    # the opposite of what every other field does. That bit us in gateway
    # mode on ECS: LLM_API_BASE_URL was correctly set on the task
    # definition (http://localhost:9000/v1) but the image-baked
    # worker_gitlab_saas.env still had the upstream Dashscope URL, and
    # the env file's value silently won — so the worker bypassed the
    # llm_gateway entirely and hit Dashscope direct. Declaring the
    # fields here restores the standard "env var overrides env file"
    # precedence (verified 2026-05-25 AWS ECS regression).
    llm_api_base_url: str = ""
    llm_model: str = ""
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
