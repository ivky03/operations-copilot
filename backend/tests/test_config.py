"""Unit tests for the real-AWS cost guardrail. No containers needed."""

import pytest
from pydantic import ValidationError

from app.config import DEV_DATABASE_URL, Settings


def make(**overrides) -> Settings:
    # _env_file=None: ignore any local .env so tests are deterministic.
    return Settings(_env_file=None, **overrides)


@pytest.mark.parametrize(
    "endpoint",
    ["http://localhost:4566", "http://127.0.0.1:4566", "http://floci:4566"],
)
def test_local_endpoints_are_allowed(endpoint: str) -> None:
    assert make(aws_endpoint_url=endpoint).aws_endpoint_url == endpoint


def test_missing_endpoint_is_rejected_because_it_means_real_aws() -> None:
    with pytest.raises(ValidationError, match="ALLOW_REAL_AWS"):
        make(aws_endpoint_url=None)


def test_real_aws_endpoint_is_rejected_without_opt_in() -> None:
    with pytest.raises(ValidationError, match="ALLOW_REAL_AWS"):
        make(aws_endpoint_url="https://s3.us-east-1.amazonaws.com")


def test_lookalike_host_is_rejected() -> None:
    # Exact host match, not substring: "localhost.evil.com" must not pass.
    with pytest.raises(ValidationError, match="ALLOW_REAL_AWS"):
        make(aws_endpoint_url="http://localhost.evil.com:4566")


def test_explicit_opt_in_allows_real_aws() -> None:
    s = make(aws_endpoint_url=None, allow_real_aws=True)
    assert s.allow_real_aws is True


def test_dev_database_is_rejected_outside_local():
    with pytest.raises(ValidationError, match="DATABASE_URL must be set"):
        # Explicit, because the test run itself sets DATABASE_URL in the environment.
        make(app_env="production", database_url=DEV_DATABASE_URL)


def test_explicit_database_is_accepted_outside_local():
    s = make(app_env="production", database_url="postgresql+psycopg://app:secret@db/prod")
    assert s.cookie_secure is True
