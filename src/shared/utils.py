"""Shared utility functions for the Kling AI Video POC pipeline."""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone


# UUID v4 pattern
_UUID_V4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# Expected S3 key patterns: property_photos/{uuid-v4}/{filename} or jobs/{uuid-v4}/{filename}
_S3_KEY_RE = re.compile(
    r"^(?:property_photos|jobs)/"
    r"([0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})"
    r"/(.+)$",
    re.IGNORECASE,
)


def parse_s3_key(key: str) -> tuple[str, str]:
    """Parse an S3 object key into (job_id, filename).

    Expected format: property_photos/{uuid-v4}/{filename}

    Returns:
        (job_id, filename) tuple

    Raises:
        ValueError: if the key does not match the expected pattern
    """
    if not key:
        raise ValueError(f"S3 key must be non-empty, got: {key!r}")

    match = _S3_KEY_RE.match(key)
    if not match:
        raise ValueError(
            f"S3 key does not match pattern 'jobs/{{uuid-v4}}/{{filename}}': {key!r}"
        )

    job_id, filename = match.group(1), match.group(2)

    if not filename:
        raise ValueError(f"S3 key has empty filename component: {key!r}")

    return job_id, filename


def validate_uuid(value: str) -> bool:
    """Return True if value is a valid UUID v4 string, False otherwise."""
    if not isinstance(value, str):
        return False
    return bool(_UUID_V4_RE.match(value))


def now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(tz=timezone.utc).isoformat()
