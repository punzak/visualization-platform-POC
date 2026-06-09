"""VideoGenerationOrchestratorFunction — Stage 4 of the Kling AI Video POC pipeline.

Triggered by EventBridge voiceover-complete event. Retrieves the story sequence
from DynamoDB, generates presigned S3 URLs for each image, submits each segment
to the configured video generation provider, and stores the returned task_id.

Supported providers (set VIDEO_PROVIDER env var):
  kling       Kling.ai API v3.0 — async, results via webhook (default)
  nova_reel   Amazon Nova Reel via Bedrock — synchronous, no webhook needed
  runway      Runway Gen-3 Alpha Turbo — synchronous polling, no webhook needed
"""
from __future__ import annotations

import os
from typing import Any

import boto3

from shared.dynamo import (
    get_story_by_job,
    safe_update_job_status,
    update_segment_kling_fields,
    update_segment_completion,
)
from shared.logger import StructuredLogger
from shared.secrets import SecretsManagerClient
from video_generation.providers.factory import get_provider

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

INPUT_BUCKET = os.environ.get("INPUT_BUCKET", "realestate-video-input")
VIDEO_PROVIDER = os.environ.get("VIDEO_PROVIDER", "kling").lower()

logger = StructuredLogger("video_generation")

# Lazy singletons — replaced in tests via module-level patching
_s3_client = None
_secrets_client = None


def _s3():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


def _secrets():
    global _secrets_client
    if _secrets_client is None:
        _secrets_client = SecretsManagerClient()
    return _secrets_client


# ---------------------------------------------------------------------------
# Lambda entry point
# ---------------------------------------------------------------------------

def handler(event: dict, context: Any) -> dict:
    """Main Lambda handler triggered by EventBridge voiceover-complete event."""
    job_id = event["detail"]["job_id"]

    logger.info(job_id=job_id, stage="video_generation", outcome="started",
                provider=VIDEO_PROVIDER)

    story = get_story_by_job(job_id)
    if story is None:
        logger.error(job_id=job_id, stage="video_generation",
                     outcome="story_not_found")
        return {"statusCode": 500}

    provider = get_provider(secrets_client=_secrets())

    for i, segment in enumerate(story.segments):
        image_url = generate_presigned_url(segment.s3_key, expires=3600)

        # Stagger submissions by 5s to avoid Nova Reel concurrency throttling
        if i > 0:
            import time
            time.sleep(5)

        result = provider.submit_task(
            image_url=image_url,
            prompt=segment.video_prompt,
            duration_seconds=segment.duration_seconds,
            camera_movement=segment.camera_movement,
            job_id=job_id,
            segment_index=segment.segment_index,
        )

        if result.is_async:
            # Async providers (Kling): store task_id, wait for webhook callback
            update_segment_kling_fields(
                story_id=story.story_id,
                segment_index=segment.segment_index,
                kling_task_id=result.task_id,
                kling_status="queued",
            )
        else:
            # Sync providers (Nova Reel, Runway): video already ready, mark complete
            update_segment_completion(
                story_id=story.story_id,
                segment_index=segment.segment_index,
                kling_status="complete",
                video_s3_key=result.video_s3_key,
            )

        logger.info(
            job_id=job_id,
            stage="video_generation",
            outcome="segment_submitted",
            segment_index=segment.segment_index,
            task_id=result.task_id,
            is_async=result.is_async,
            provider=VIDEO_PROVIDER,
        )

    safe_update_job_status(job_id, "generating", "assembling")
    logger.info(job_id=job_id, stage="video_generation", outcome="completed",
                provider=VIDEO_PROVIDER)
    return {"statusCode": 200}


# ---------------------------------------------------------------------------
# Presigned URL helper (shared by all providers)
# ---------------------------------------------------------------------------

def generate_presigned_url(s3_key: str, expires: int = 3600) -> str:
    """Generate a presigned S3 GET URL for the given key.

    Args:
        s3_key: S3 object key in the input bucket.
        expires: URL expiry in seconds. Must be in [3600, 604800].

    Returns:
        Presigned HTTPS URL string.

    Raises:
        ValueError: if expires is outside [3600, 604800].
    """
    if not (3600 <= expires <= 604800):
        raise ValueError(
            f"expires must be between 3600 and 604800, got {expires}"
        )
    return _s3().generate_presigned_url(
        "get_object",
        Params={"Bucket": INPUT_BUCKET, "Key": s3_key},
        ExpiresIn=expires,
    )
