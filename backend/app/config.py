"""Application settings, loaded from environment variables (and an optional .env file).

Why pydantic-settings: every setting is typed and validated at startup, so a missing or
malformed value crashes immediately instead of failing later in the middle of a request.
"""

from functools import lru_cache
from pathlib import Path
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

    # Language models. Keys are SecretStr: printing or logging settings shows '**********'.
    # Groq is the primary provider; Gemini becomes the fallback only if its key is set.
    groq_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    llm_planner_model: str = "openai/gpt-oss-120b"
    llm_writer_model: str = "openai/gpt-oss-120b"
    llm_fallback_model: str = "gemini-2.5-flash"  # used with the Gemini key
    llm_timeout_seconds: float = 45.0

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
