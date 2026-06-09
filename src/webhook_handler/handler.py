"""KlingWebhookHandlerFunction — Stage 5 of the Kling AI Video POC pipeline.

Triggered by API Gateway POST /webhook/kling. Validates HMAC-SHA256 signature,
downloads completed video segments from Kling CDN, stores them in S3, updates
DynamoDB, and emits all-segments-complete when all segments are done.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Any

import boto3
import requests

from shared.dynamo import (
    query_segment_by_task_id,
    query_segments_by_job,
    update_segment_completion,
)
from shared.logger import StructuredLogger
from shared.secrets import SecretsManagerClient
from shared.xray import begin_subsegment, end_subsegment, put_annotation

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

EVENT_BUS_NAME = os.environ.get("EVENT_BUS_NAME", "realestate-video-pipeline")
ASSETS_BUCKET = os.environ.get("ASSETS_BUCKET", "realestate-video-assets")
KLING_WEBHOOK_SECRET_ID = os.environ.get(
    "KLING_WEBHOOK_SECRET_ID", "kling/webhook_secret"
)

logger = StructuredLogger("webhook_handler")

# Lazy singletons — replaced in tests via module-level patching
_s3_client = None
_eb_client = None
_secrets_client = None


def _s3():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


def _eb():
    global _eb_client
    if _eb_client is None:
        _eb_client = boto3.client("events")
    return _eb_client


def _secrets():
    global _secrets_client
    if _secrets_client is None:
        _secrets_client = SecretsManagerClient()
    return _secrets_client

# ---------------------------------------------------------------------------
# Lambda entry point
# ---------------------------------------------------------------------------


def handler(event: dict, context: Any) -> dict:
    """Main Lambda handler triggered by API Gateway POST /webhook/kling."""
    body_str: str = event.get("body") or ""
    headers: dict = event.get("headers") or {}
    signature: str = headers.get("X-Kling-Signature", "")

    # Retrieve webhook secret
    secret_data = _secrets().get_secret(KLING_WEBHOOK_SECRET_ID)
    webhook_secret: str = secret_data["webhook_secret"]

    # Validate HMAC signature
    if not validate_webhook_signature(body_str, signature, webhook_secret):
        source_ip = (event.get("requestContext") or {}).get("identity", {}).get(
            "sourceIp", "unknown"
        )
        logger.warning(
            job_id="unknown",
            stage="webhook_handler",
            outcome="invalid_signature",
            source_ip=source_ip,
        )
        return {"statusCode": 401, "body": "Invalid signature"}

    # Parse payload
    try:
        payload = json.loads(body_str)
    except (json.JSONDecodeError, ValueError):
        logger.warning(
            job_id="unknown",
            stage="webhook_handler",
            outcome="invalid_json_body",
        )
        return {"statusCode": 400, "body": "Invalid JSON body"}

    task_id: str = payload.get("task_id", "")
    status: str = payload.get("status", "")

    # Look up segment by task_id
    story_item = query_segment_by_task_id(task_id)
    if story_item is None:
        logger.warning(
            job_id="unknown",
            stage="webhook_handler",
            outcome="unknown_task_id",
            task_id=task_id,
        )
        return {"statusCode": 400, "body": "Unknown task_id"}

    job_id = get_job_id_from_segment(story_item)
    story_id: str = story_item.get("story_id", "")

    # Find the matching segment within the story
    segments = story_item.get("segments", [])
    if isinstance(segments, str):
        segments = json.loads(segments)

    segment_item = None
    for seg in segments:
        if seg.get("kling_task_id") == task_id:
            segment_item = seg
            break

    if segment_item is None:
        logger.warning(
            job_id=job_id,
            stage="webhook_handler",
            outcome="segment_not_found",
            task_id=task_id,
        )
        return {"statusCode": 400, "body": "Unknown task_id"}

    segment_index: int = int(segment_item.get("segment_index", 0))

    if status == "completed":
        # Idempotency: skip if already complete
        if segment_item.get("kling_status") == "complete":
            logger.info(
                job_id=job_id,
                stage="webhook_handler",
                outcome="already_complete_skipped",
                task_id=task_id,
            )
            return {"statusCode": 200}

        video_url: str = payload.get("video_url", "")
        s3_key = download_video_segment(video_url, job_id, segment_index)

        update_segment_completion(
            story_id=story_id,
            segment_index=segment_index,
            kling_status="complete",
            video_s3_key=s3_key,
        )

        logger.info(
            job_id=job_id,
            stage="webhook_handler",
            outcome="segment_complete",
            segment_index=segment_index,
            s3_key=s3_key,
        )

        if check_all_segments_complete(job_id):
            emit_all_segments_complete(job_id)
            logger.info(
                job_id=job_id,
                stage="webhook_handler",
                outcome="all_segments_complete",
            )

    elif status == "failed":
        error_message: str = payload.get("error_message", "unknown")

        # Store error_message in the segment via a custom update
        _update_segment_failed(story_id, segment_index, error_message)

        logger.warning(
            job_id=job_id,
            stage="webhook_handler",
            outcome="segment_failed",
            segment_index=segment_index,
            error_message=error_message,
        )

    return {"statusCode": 200}

# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------


def validate_webhook_signature(payload_body: str, signature: str, secret: str) -> bool:
    """Validate HMAC-SHA256 signature of the webhook payload.

    Args:
        payload_body: Raw request body string.
        signature: Signature from X-Kling-Signature header.
        secret: Shared HMAC secret.

    Returns:
        True if signature is valid, False otherwise.
    """
    if not signature:
        return False
    expected = hmac.new(
        secret.encode("utf-8"),
        payload_body.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def download_video_segment(video_url: str, job_id: str, segment_index: int) -> str:
    """Download video from Kling CDN and upload to S3 assets bucket.

    Args:
        video_url: CDN URL for the completed video segment.
        job_id: Pipeline job identifier.
        segment_index: Segment position index.

    Returns:
        S3 key where the video was stored.
    """
    begin_subsegment("kling-cdn-download")
    put_annotation("job_id", job_id)
    put_annotation("segment_index", segment_index)
    try:
        response = requests.get(video_url, timeout=25)
        response.raise_for_status()
        video_bytes = response.content
    finally:
        end_subsegment()

    s3_key = f"segments/{job_id}/{segment_index}.mp4"
    _s3().put_object(
        Bucket=ASSETS_BUCKET,
        Key=s3_key,
        Body=video_bytes,
        ContentType="video/mp4",
    )
    return s3_key


def check_all_segments_complete(job_id: str) -> bool:
    """Check whether all segments for a job have kling_status == 'complete'.

    Args:
        job_id: Pipeline job identifier.

    Returns:
        True only if every segment has kling_status == 'complete'.
    """
    segments = query_segments_by_job(job_id)
    if not segments:
        return False
    return all(seg.get("kling_status") == "complete" for seg in segments)


def emit_all_segments_complete(job_id: str) -> None:
    """Emit all-segments-complete EventBridge event.

    Args:
        job_id: Pipeline job identifier.
    """
    _eb().put_events(
        Entries=[
            {
                "Source": "realestate.video.pipeline",
                "DetailType": "all-segments-complete",
                "Detail": json.dumps({"job_id": job_id}),
                "EventBusName": EVENT_BUS_NAME,
            }
        ]
    )


def get_job_id_from_segment(segment_item: dict) -> str:
    """Extract job_id from the story item returned by query_segment_by_task_id.

    Args:
        segment_item: Story item dict from DynamoDB.

    Returns:
        job_id string.
    """
    return segment_item["job_id"]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _update_segment_failed(story_id: str, segment_index: int, error_message: str) -> None:
    """Update a segment to failed status with error_message."""
    from shared import dynamo as _dynamo
    import json as _json

    story = _dynamo._get_story_raw(story_id)
    if story is None:
        return

    segs = story.get("segments", [])
    if isinstance(segs, str):
        segs = _json.loads(segs)

    for seg in segs:
        if int(seg.get("segment_index", -1)) == segment_index:
            seg["kling_status"] = "failed"
            seg["error_message"] = error_message
            break

    _dynamo._stories_table().update_item(
        Key={"story_id": story_id},
        UpdateExpression="SET #segs = :segs",
        ExpressionAttributeNames={"#segs": "segments"},
        ExpressionAttributeValues={":segs": _json.dumps(segs)},
    )
