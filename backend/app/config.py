"""Application settings, loaded from environment variables (and an optional .env file).

Why pydantic-settings: every setting is typed and validated at startup, so a missing or
malformed value crashes immediately instead of failing later in the middle of a request.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo-root .env, regardless of which directory the process starts in.
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"

# Development-only database URL. Fine on a laptop; never acceptable on a real server.
DEV_DATABASE_URL = "postgresql+psycopg://copilot:copilot-dev-only@localhost:5433/copilot"

# Hosts we treat as "local emulator". Anything else counts as real AWS.
LOCAL_AWS_HOSTS = {"localhost", "127.0.0.1", "floci", "host.docker.internal"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    app_env: str = "local"  # "local", "test" or "production"

    # Postgres. Host port 5433 matches docker-compose.yml.
    database_url: str = DEV_DATABASE_URL

    # Login sessions
    session_ttl_hours: int = 8  # absolute lifetime; the user logs in again after this
    session_cookie_name: str = "oc_session"

    # AWS (Floci locally). Credentials can be any non-empty value for Floci.
    aws_endpoint_url: str | None = "http://localhost:4566"
    aws_region: str = "us-east-1"
    aws_access_key_id: str = "test"
    aws_secret_access_key: str = "test"  # noqa: S105 - dummy value for the local emulator

    # Cost guardrail: real (billable) AWS must be enabled explicitly.
    allow_real_aws: bool = False

    # Ingestion (M6). Uploaded files go to the bucket; each upload becomes one job, delivered
    # to the worker through the queue. Tests use their own names so a running dev worker
    # can't pick up test messages.
    upload_bucket: str = "opscopilot-uploads"
    ingest_queue_name: str = "opscopilot-ingest"
    ingest_dlq_name: str = "opscopilot-ingest-dlq"
    # How long a received message stays hidden from other workers while one works on it.
    # Also how long a job stays locked: a worker that dies frees its job after this.
    ingest_visibility_timeout: int = 120
    ingest_max_attempts: int = 3  # failed attempts before a job is marked failed
    # SQS moves a message to the dead-letter queue after this many receives without a delete,
    # i.e. when the worker keeps dying before it can record anything. Above max_attempts, so
    # ordinary failures are counted by our code first.
    ingest_max_receives: int = 5
    # A pending job with no message for this long is re-sent (its first send was lost).
    ingest_stale_after_seconds: int = 120
    max_upload_bytes: int = 20 * 1024 * 1024

    # Language models. Keys are SecretStr: printing or logging settings shows '**********'.
    # Groq is the primary provider; Gemini becomes the fallback only if its key is set.
    groq_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    llm_planner_model: str = "openai/gpt-oss-120b"
    llm_writer_model: str = "openai/gpt-oss-120b"
    # Fallback: chosen from live probes (2026-11-18): answered in ~0.5 s while the larger
    # free-tier Flash models returned 503 "high demand" or timed out. Change here if that shifts.
    llm_fallback_model: str = "gemini-3.5-flash-lite"
    llm_timeout_seconds: float = 45.0

    # Answer cache (app/answer/cache.py). Keys change whenever a readable source gets a new
    # version, so the lifetime only bounds how long a model's phrasing is reused.
    answer_cache_enabled: bool = True
    answer_cache_ttl_hours: int = 24

    # Which tables the planner is shown (app/answer/schema_retrieval.py): "full" describes every
    # readable table; "retrieved" describes the `schema_top_k` most similar plus the tables they
    # need. "full" stays the default until the M8c experiment measures both with real models.
    schema_mode: Literal["full", "retrieved"] = "full"
    schema_top_k: int = 5

    # Document embeddings run locally on the CPU (no API, no quota). The model (~64 MB) is
    # downloaded once into the cache directory.
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_cache_dir: str = str(Path.home() / ".cache" / "fastembed")

    @property
    def is_local(self) -> bool:
        return self.app_env in {"local", "test"}

    @property
    def cookie_secure(self) -> bool:
        """Send cookies over HTTPS only, everywhere except local development."""
        return not self.is_local

    @model_validator(mode="after")
    def _guard_against_dev_database_outside_local(self) -> "Settings":
        # Fail fast: a real server silently using the dev password would be a quiet disaster.
        if not self.is_local and self.database_url == DEV_DATABASE_URL:
            raise ValueError("DATABASE_URL must be set explicitly when APP_ENV is not local.")
        return self

    @model_validator(mode="after")
    def _guard_against_real_aws(self) -> "Settings":
        # Raising ValueError here makes pydantic raise ValidationError at startup,
        # so the app refuses to boot instead of silently calling (and billing) real AWS.
        if self.allow_real_aws:
            return self
        if not self.aws_endpoint_url:
            # No endpoint override means boto3 would talk to real AWS.
            raise ValueError(
                "AWS_ENDPOINT_URL is unset, which would target real AWS. "
                "Set ALLOW_REAL_AWS=true only if you intend to use (and pay for) real AWS."
            )
        host = urlparse(self.aws_endpoint_url).hostname or ""
        if host not in LOCAL_AWS_HOSTS:
            raise ValueError(
                f"AWS_ENDPOINT_URL host '{host}' is not a local emulator host "
                f"{sorted(LOCAL_AWS_HOSTS)}. Set ALLOW_REAL_AWS=true to override deliberately."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """Single cached Settings instance. Tests can call get_settings.cache_clear()."""
    return Settings()
