"""Application settings, loaded from environment variables (and an optional .env file).

Why pydantic-settings: every setting is typed and validated at startup, so a missing or
malformed value crashes immediately instead of failing later in the middle of a request.
"""

from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo-root .env, regardless of which directory the process starts in.
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"

# Hosts we treat as "local emulator". Anything else counts as real AWS.
LOCAL_AWS_HOSTS = {"localhost", "127.0.0.1", "floci", "host.docker.internal"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    app_env: str = "local"

    # Postgres. Host port 5433 matches docker-compose.yml.
    database_url: str = "postgresql+psycopg://copilot:copilot-dev-only@localhost:5433/copilot"

    # AWS (Floci locally). Credentials can be any non-empty value for Floci.
    aws_endpoint_url: str | None = "http://localhost:4566"
    aws_region: str = "us-east-1"
    aws_access_key_id: str = "test"
    aws_secret_access_key: str = "test"  # noqa: S105 - dummy value for the local emulator

    # Cost guardrail: real (billable) AWS must be enabled explicitly.
    allow_real_aws: bool = False

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
