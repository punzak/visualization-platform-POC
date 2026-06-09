"""DynamoDB access layer for the Kling AI Video POC pipeline."""
from __future__ import annotations

import json
import os
from decimal import Decimal
from typing import Optional

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from shared.models import ImageAnalysisResult, JobRecord, SceneSegment, StorySequence


def _floats_to_decimals(obj):
    """Recursively convert float values to Decimal for DynamoDB compatibility."""
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: _floats_to_decimals(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_floats_to_decimals(v) for v in obj]
    return obj


def _decimals_to_floats(obj):
    """Recursively convert Decimal values back to float after reading from DynamoDB."""
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: _decimals_to_floats(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decimals_to_floats(v) for v in obj]
    return obj


# Table name environment variables with defaults matching the design
JOBS_TABLE = os.environ.get("JOBS_TABLE", "property-video-jobs")
IMAGES_TABLE = os.environ.get("IMAGES_TABLE", "property-video-images")
STORIES_TABLE = os.environ.get("STORIES_TABLE", "property-video-stories")

_dynamodb = boto3.resource("dynamodb")


def _jobs_table():
    return _dynamodb.Table(JOBS_TABLE)


def _images_table():
    return _dynamodb.Table(IMAGES_TABLE)


def _stories_table():
    return _dynamodb.Table(STORIES_TABLE)


# ---------------------------------------------------------------------------
# Jobs table
# ---------------------------------------------------------------------------

def get_job(job_id: str) -> Optional[JobRecord]:
    """Retrieve a JobRecord by job_id. Returns None if not found."""
    response = _jobs_table().get_item(Key={"job_id": job_id})
    item = response.get("Item")
    if item is None:
        return None
    return JobRecord.from_dict(item)


def put_job(job: JobRecord) -> None:
    """Persist a JobRecord. Validates image_count is between 1 and 20."""
    if not (1 <= job.image_count <= 20):
        raise ValueError(
            f"image_count must be between 1 and 20, got {job.image_count}"
        )
    _jobs_table().put_item(Item=job.to_dict())


def update_job_status(job_id: str, new_status: str) -> None:
    """Unconditionally update the status field of a JobRecord."""
    _jobs_table().update_item(
        Key={"job_id": job_id},
        UpdateExpression="SET #s = :new_status",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":new_status": new_status},
    )


def safe_update_job_status(
    job_id: str, expected_current: str, new_status: str
) -> None:
    """Update job status only if the current status matches expected_current.

    Uses a DynamoDB conditional expression to prevent status regression.
    Raises ClientError with code ConditionalCheckFailedException if the
    current status does not match expected_current.
    """
    _jobs_table().update_item(
        Key={"job_id": job_id},
        UpdateExpression="SET #s = :new_status",
        ConditionExpression="attribute_exists(job_id) AND #s = :expected",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":new_status": new_status,
            ":expected": expected_current,
        },
    )


def increment_images_analyzed(job_id: str) -> dict:
    """Atomically increment images_analyzed using DynamoDB ADD. Returns the updated item."""
    response = _jobs_table().update_item(
        Key={"job_id": job_id},
        UpdateExpression="ADD images_analyzed :one",
        ExpressionAttributeValues={":one": 1},
        ReturnValues="ALL_NEW",
    )
    return response["Attributes"]


def update_job_fields(job_id: str, **fields) -> None:
    """Generic field updater for a JobRecord."""
    if not fields:
        return
    set_parts = []
    expr_names: dict = {}
    expr_values: dict = {}
    for i, (key, value) in enumerate(fields.items()):
        placeholder_name = f"#f{i}"
        placeholder_value = f":v{i}"
        set_parts.append(f"{placeholder_name} = {placeholder_value}")
        expr_names[placeholder_name] = key
        expr_values[placeholder_value] = value
    _jobs_table().update_item(
        Key={"job_id": job_id},
        UpdateExpression="SET " + ", ".join(set_parts),
        ExpressionAttributeNames=expr_names,
        ExpressionAttributeValues=expr_values,
    )


# ---------------------------------------------------------------------------
# Images table
# ---------------------------------------------------------------------------

def put_image_result(result: ImageAnalysisResult) -> None:
    """Persist an ImageAnalysisResult to the images table."""
    _images_table().put_item(Item=_floats_to_decimals(result.to_dict()))


def query_images_by_job(job_id: str) -> list[ImageAnalysisResult]:
    """Query all ImageAnalysisResult records for a job using the job_id-index GSI."""
    response = _images_table().query(
        IndexName="job_id-index",
        KeyConditionExpression=Key("job_id").eq(job_id),
    )
    return [
        ImageAnalysisResult.from_dict(_decimals_to_floats(item))
        for item in response.get("Items", [])
    ]


# ---------------------------------------------------------------------------
# Stories table
# ---------------------------------------------------------------------------

def put_story_sequence(story: StorySequence) -> None:
    """Persist a StorySequence, storing segments as a JSON list."""
    item = story.to_dict()
    # Ensure segments are stored as a JSON string for DynamoDB compatibility
    item["segments"] = json.dumps(
        [s.to_dict() if isinstance(s, SceneSegment) else s for s in story.segments]
    )
    _stories_table().put_item(Item=item)


def get_story_by_job(job_id: str) -> Optional[StorySequence]:
    """Retrieve a StorySequence by job_id using the job_id-index GSI."""
    response = _stories_table().query(
        IndexName="job_id-index",
        KeyConditionExpression=Key("job_id").eq(job_id),
    )
    items = response.get("Items", [])
    if not items:
        return None
    item = items[0]
    # Deserialize segments from JSON string if needed
    if isinstance(item.get("segments"), str):
        item["segments"] = json.loads(item["segments"])
    return StorySequence.from_dict(item)


def update_segment_kling_fields(
    story_id: str, segment_index: int, kling_task_id: str, kling_status: str
) -> None:
    """Update kling_task_id and kling_status for a specific segment within a story.

    Also sets the top-level ``kling_task_id`` attribute so the
    ``kling_task_id-index`` GSI can locate this story item by task ID.
    """
    story = _get_story_raw(story_id)
    if story is None:
        raise ValueError(f"Story {story_id} not found")
    segments = _load_segments(story)
    for seg in segments:
        if int(seg.get("segment_index", -1)) == segment_index:
            seg["kling_task_id"] = kling_task_id
            seg["kling_status"] = kling_status
            break
    _stories_table().update_item(
        Key={"story_id": story_id},
        UpdateExpression="SET #segs = :segs, #ktid = :ktid",
        ExpressionAttributeNames={
            "#segs": "segments", "#ktid": "kling_task_id"},
        ExpressionAttributeValues={":segs": json.dumps(
            segments), ":ktid": kling_task_id},
    )


def update_segment_completion(
    story_id: str,
    segment_index: int,
    kling_status: str,
    video_s3_key: Optional[str] = None,
) -> None:
    """Update kling_status (and optionally video_s3_key) for a specific segment."""
    story = _get_story_raw(story_id)
    if story is None:
        raise ValueError(f"Story {story_id} not found")
    segments = _load_segments(story)
    for seg in segments:
        if int(seg.get("segment_index", -1)) == segment_index:
            seg["kling_status"] = kling_status
            if video_s3_key is not None:
                seg["video_s3_key"] = video_s3_key
            break
    _stories_table().update_item(
        Key={"story_id": story_id},
        UpdateExpression="SET #segs = :segs",
        ExpressionAttributeNames={"#segs": "segments"},
        ExpressionAttributeValues={":segs": json.dumps(segments)},
    )


def query_segments_by_job(job_id: str) -> list[dict]:
    """Return raw segment dicts from the story for a given job_id."""
    story = get_story_by_job(job_id)
    if story is None:
        return []
    return [
        s.to_dict() if isinstance(s, SceneSegment) else s for s in story.segments
    ]


def query_segment_by_task_id(task_id: str) -> Optional[dict]:
    """Find a story item containing a segment with the given kling_task_id.

    First tries the ``kling_task_id-index`` GSI (fast path, works when the
    top-level attribute is set). Falls back to a full table scan searching
    inside the segments JSON blob (handles multi-segment stories where the
    GSI only holds the last-written task_id).
    """
    # Fast path: GSI lookup
    response = _stories_table().query(
        IndexName="kling_task_id-index",
        KeyConditionExpression=Key("kling_task_id").eq(task_id),
    )
    items = response.get("Items", [])
    if items:
        item = items[0]
        if isinstance(item.get("segments"), str):
            item["segments"] = json.loads(item["segments"])
        return item

    # Fallback: scan all stories and search inside segments JSON
    scan_response = _stories_table().scan()
    for item in scan_response.get("Items", []):
        segs = item.get("segments", [])
        if isinstance(segs, str):
            segs = json.loads(segs)
        for seg in segs:
            if seg.get("kling_task_id") == task_id:
                item["segments"] = segs
                return item
    return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_story_raw(story_id: str) -> Optional[dict]:
    response = _stories_table().get_item(Key={"story_id": story_id})
    return response.get("Item")


def _load_segments(story_item: dict) -> list[dict]:
    segs = story_item.get("segments", [])
    if isinstance(segs, str):
        return json.loads(segs)
    return list(segs)
