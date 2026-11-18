"""AWS clients (S3, SQS) pointed at Floci locally.

Every boto3 client is created here, so the endpoint and the real-AWS guard in config.py
apply everywhere. No other module should call boto3.client() directly.
"""

from functools import lru_cache
from typing import Any

import boto3
from botocore.config import Config

from app.config import get_settings

# Short timeouts and few retries: a health check must answer quickly, not hang.
_BOTO_CONFIG = Config(connect_timeout=2, read_timeout=5, retries={"max_attempts": 2})


@lru_cache
def get_client(service: str) -> Any:
    s = get_settings()
    return boto3.client(
        service,
        endpoint_url=s.aws_endpoint_url,
        region_name=s.aws_region,
        aws_access_key_id=s.aws_access_key_id,
        aws_secret_access_key=s.aws_secret_access_key,
        config=_BOTO_CONFIG,
    )


def check_aws() -> dict[str, str]:
    """Verify S3 and SQS respond. Read-only calls; creates nothing."""
    get_client("s3").list_buckets()
    get_client("sqs").list_queues()
    return {"status": "ok", "endpoint": get_settings().aws_endpoint_url or "aws"}
