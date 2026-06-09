"""Unit tests for StorySequencingFunction using moto."""
from __future__ import annotations

# ruff: noqa
# fmt: off
import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src")))
os.environ.setdefault("JOBS_TABLE", "property-video-jobs")
os.environ.setdefault("IMAGES_TABLE", "property-video-images")
os.environ.setdefault("STORIES_TABLE", "property-video-stories")
os.environ.setdefault("EVENT_BUS_NAME", "realestate-video-pipeline")
os.environ.setdefault("BEDROCK_REGION", "us-east-1")

import copy
import json
import uuid
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

import shared.dynamo as dynamo
import importlib.util

_spec = importlib.util.spec_from_file_location(
    "story_sequencing.handler",
    os.path.join(os.path.dirname(__file__), "..", "..", "src", "story_sequencing", "handler.py"),
)
ss = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ss)

from shared.models import ImageAnalysisResult, JobRecord


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_JOB_ID = "a1b2c3d4-e5f6-4789-abcd-ef0123456789"
EVENT_BUS = "realestate-video-pipeline"

IMAGE_ID_1 = "img-0001-0000-0000-0000-000000000001"
IMAGE_ID_2 = "img-0002-0000-0000-0000-000000000002"
S3_KEY_1 = f"property_photos/{VALID_JOB_ID}/living_room.jpg"
S3_KEY_2 = f"property_photos/{VALID_JOB_ID}/kitchen.jpg"


def _make_valid_story_json(image_ids_and_keys: list[tuple[str, str]], total_duration: int = 70) -> dict:
    """Build a valid story JSON matching the given images."""
    segments = []
    for i, (image_id, s3_key) in enumerate(image_ids_and_keys):
        segments.append({
            "segment_index": i,
            "image_id": image_id,
            "s3_key": s3_key,
            "script_text": f"Welcome to this beautiful scene {i + 1}.",
            "video_prompt": f"Cinematic slow zoom into the {i + 1} room.",
            "duration_seconds": total_duration // len(image_ids_and_keys),
            "camera_movement": "slow_zoom_in",
        })
    return {
        "full_script": "Welcome to this stunning property.",
        "total_duration_seconds": total_duration,
        "segments": segments,
    }


def _make_bedrock_response(story_dict: dict) -> MagicMock:
    """Return a mock Bedrock client that returns the given story JSON."""
    mock_body = MagicMock()
    mock_body.read.return_value = json.dumps({
        "content": [{"type": "text", "text": json.dumps(story_dict)}]
    }).encode()
    mock_client = MagicMock()
    mock_client.invoke_model.return_value = {"body": mock_body}
    return mock_client


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def aws_credentials(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@pytest.fixture()
def aws_resources():
    """Spin up moto-backed DynamoDB and EventBridge resources."""
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name="us-east-1")

        ddb.create_table(
            TableName="property-video-jobs",
            KeySchema=[{"AttributeName": "job_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "job_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb.create_table(
            TableName="property-video-images",
            KeySchema=[{"AttributeName": "image_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "image_id", "AttributeType": "S"},
                {"AttributeName": "job_id", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "job_id-index",
                    "KeySchema": [{"AttributeName": "job_id", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb.create_table(
            TableName="property-video-stories",
            KeySchema=[{"AttributeName": "story_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "story_id", "AttributeType": "S"},
                {"AttributeName": "job_id", "AttributeType": "S"},
                {"AttributeName": "kling_task_id", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "job_id-index",
                    "KeySchema": [{"AttributeName": "job_id", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "kling_task_id-index",
                    "KeySchema": [{"AttributeName": "kling_task_id", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        events = boto3.client("events", region_name="us-east-1")
        events.create_event_bus(Name=EVENT_BUS)

        dynamo._dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        ss._events_client = boto3.client("events", region_name="us-east-1")

        yield {"ddb": ddb, "events": events}

        ss._bedrock_client = None
        ss._events_client = None


def _put_job(job_id: str, image_count: int = 2) -> None:
    job = JobRecord(
        job_id=job_id,
        status="sequencing",
        created_at="2024-01-01T00:00:00+00:00",
        updated_at="2024-01-01T00:00:00+00:00",
        property_address="123 Main St",
        image_count=image_count,
    )
    dynamo.put_job(job)


def _put_images(job_id: str, image_ids_and_keys: list[tuple[str, str]]) -> None:
    for image_id, s3_key in image_ids_and_keys:
        result = ImageAnalysisResult(
            image_id=image_id,
            job_id=job_id,
            s3_key=s3_key,
            sequence_index=0,
            room_type="living_room",
            architectural_style="modern",
            key_selling_points=["natural light"],
            lighting_quality="excellent",
            ambiance="warm",
            composition_score=0.85,
            analysis_timestamp="2024-01-01T00:00:00+00:00",
        )
        dynamo.put_image_result(result)


def _make_event(job_id: str) -> dict:
    return {"detail": {"job_id": job_id}}


# ---------------------------------------------------------------------------
# Test: valid EventBridge event triggers DynamoDB query and Bedrock invocation
# ---------------------------------------------------------------------------

def test_valid_event_triggers_dynamo_query_and_bedrock(aws_resources):
    images = [(IMAGE_ID_1, S3_KEY_1), (IMAGE_ID_2, S3_KEY_2)]
    _put_job(VALID_JOB_ID, image_count=2)
    _put_images(VALID_JOB_ID, images)

    story_json = _make_valid_story_json(images)
    mock_bedrock = _make_bedrock_response(story_json)
    ss._bedrock_client = mock_bedrock

    result = ss.handler(_make_event(VALID_JOB_ID), None)

    assert result == {"statusCode": 200}
    mock_bedrock.invoke_model.assert_called_once()

    # Story should be persisted in DynamoDB
    story = dynamo.get_story_by_job(VALID_JOB_ID)
    assert story is not None
    assert story.job_id == VALID_JOB_ID
    assert len(story.segments) == 2
    assert story.total_duration_seconds == 70


# ---------------------------------------------------------------------------
# Test: invalid duration < 60 raises ValueError
# ---------------------------------------------------------------------------

def test_invalid_duration_below_60_raises(aws_resources):
    images = [(IMAGE_ID_1, S3_KEY_1), (IMAGE_ID_2, S3_KEY_2)]
    _put_job(VALID_JOB_ID, image_count=2)
    _put_images(VALID_JOB_ID, images)

    story_json = _make_valid_story_json(images, total_duration=59)
    ss._bedrock_client = _make_bedrock_response(story_json)

    with pytest.raises(ValueError, match="total_duration_seconds"):
        ss.sequence_images(VALID_JOB_ID)


# ---------------------------------------------------------------------------
# Test: invalid duration > 90 raises ValueError
# ---------------------------------------------------------------------------

def test_invalid_duration_above_90_raises(aws_resources):
    images = [(IMAGE_ID_1, S3_KEY_1), (IMAGE_ID_2, S3_KEY_2)]
    _put_job(VALID_JOB_ID, image_count=2)
    _put_images(VALID_JOB_ID, images)

    story_json = _make_valid_story_json(images, total_duration=91)
    ss._bedrock_client = _make_bedrock_response(story_json)

    with pytest.raises(ValueError, match="total_duration_seconds"):
        ss.sequence_images(VALID_JOB_ID)


# ---------------------------------------------------------------------------
# Test: segment count mismatch raises ValueError
# ---------------------------------------------------------------------------

def test_segment_count_mismatch_raises(aws_resources):
    images = [(IMAGE_ID_1, S3_KEY_1), (IMAGE_ID_2, S3_KEY_2)]
    _put_job(VALID_JOB_ID, image_count=2)
    _put_images(VALID_JOB_ID, images)

    # Return only 1 segment for 2 images
    story_json = _make_valid_story_json([(IMAGE_ID_1, S3_KEY_1)], total_duration=70)
    ss._bedrock_client = _make_bedrock_response(story_json)

    with pytest.raises(ValueError, match="Segment count"):
        ss.sequence_images(VALID_JOB_ID)


# ---------------------------------------------------------------------------
# Test: story-generated event emitted with correct job_id and story_id
# ---------------------------------------------------------------------------

def test_story_generated_event_emitted(aws_resources):
    images = [(IMAGE_ID_1, S3_KEY_1), (IMAGE_ID_2, S3_KEY_2)]
    _put_job(VALID_JOB_ID, image_count=2)
    _put_images(VALID_JOB_ID, images)

    story_json = _make_valid_story_json(images)
    ss._bedrock_client = _make_bedrock_response(story_json)

    with patch.object(ss, "emit_story_generated") as mock_emit:
        ss.handler(_make_event(VALID_JOB_ID), None)
        mock_emit.assert_called_once()
        call_args = mock_emit.call_args
        assert call_args[0][0] == VALID_JOB_ID  # job_id
        assert isinstance(call_args[0][1], str)  # story_id is a string UUID


# ---------------------------------------------------------------------------
# Test: camera_movement must be in VALID_CAMERA_MOVEMENTS
# ---------------------------------------------------------------------------

def test_invalid_camera_movement_raises(aws_resources):
    images = [(IMAGE_ID_1, S3_KEY_1), (IMAGE_ID_2, S3_KEY_2)]
    _put_job(VALID_JOB_ID, image_count=2)
    _put_images(VALID_JOB_ID, images)

    story_json = _make_valid_story_json(images)
    # Corrupt the camera_movement on the first segment
    story_json["segments"][0]["camera_movement"] = "invalid_movement"
    ss._bedrock_client = _make_bedrock_response(story_json)

    with pytest.raises(ValueError, match="camera_movement"):
        ss.sequence_images(VALID_JOB_ID)


def test_all_valid_camera_movements_accepted(aws_resources):
    """Each valid camera movement should be accepted without error."""
    for movement in ss.VALID_CAMERA_MOVEMENTS:
        images = [(IMAGE_ID_1, S3_KEY_1)]
        job_id = str(uuid.uuid4())
        _put_job(job_id, image_count=1)
        _put_images(job_id, images)

        story_json = _make_valid_story_json(images, total_duration=65)
        story_json["segments"][0]["camera_movement"] = movement
        ss._bedrock_client = _make_bedrock_response(story_json)

        story = ss.sequence_images(job_id)
        assert story.segments[0].camera_movement == movement


# ---------------------------------------------------------------------------
# Test: parse_story_response handles markdown fences
# ---------------------------------------------------------------------------

def test_parse_story_response_plain_json():
    story_dict = {"full_script": "Hello", "total_duration_seconds": 70, "segments": []}
    body = {"content": [{"type": "text", "text": json.dumps(story_dict)}]}
    result = ss.parse_story_response(body)
    assert result["full_script"] == "Hello"
    assert result["total_duration_seconds"] == 70


def test_parse_story_response_strips_markdown_fences():
    story_dict = {"full_script": "Hello", "total_duration_seconds": 70, "segments": []}
    text = "```json\n" + json.dumps(story_dict) + "\n```"
    body = {"content": [{"type": "text", "text": text}]}
    result = ss.parse_story_response(body)
    assert result["full_script"] == "Hello"


def test_parse_story_response_strips_plain_code_fences():
    story_dict = {"full_script": "Test", "total_duration_seconds": 75, "segments": []}
    text = "```\n" + json.dumps(story_dict) + "\n```"
    body = {"content": [{"type": "text", "text": text}]}
    result = ss.parse_story_response(body)
    assert result["total_duration_seconds"] == 75


def test_parse_story_response_no_text_raises():
    with pytest.raises(ValueError, match="No text content"):
        ss.parse_story_response({"content": []})


# ---------------------------------------------------------------------------
# Test: job status updated to voiceover after successful sequencing
# ---------------------------------------------------------------------------

def test_job_status_updated_to_voiceover(aws_resources):
    images = [(IMAGE_ID_1, S3_KEY_1), (IMAGE_ID_2, S3_KEY_2)]
    _put_job(VALID_JOB_ID, image_count=2)
    _put_images(VALID_JOB_ID, images)

    story_json = _make_valid_story_json(images)
    ss._bedrock_client = _make_bedrock_response(story_json)

    ss.handler(_make_event(VALID_JOB_ID), None)

    job = dynamo.get_job(VALID_JOB_ID)
    assert job.status == "voiceover"
    assert job.story_sequence_id is not None
