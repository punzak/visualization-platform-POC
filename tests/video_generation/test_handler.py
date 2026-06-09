"""Unit tests for VideoGenerationOrchestratorFunction using moto and unittest.mock."""
from __future__ import annotations

# ruff: noqa
# fmt: off
import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src")))
os.environ.setdefault("JOBS_TABLE", "property-video-jobs")
os.environ.setdefault("IMAGES_TABLE", "property-video-images")
os.environ.setdefault("STORIES_TABLE", "property-video-stories")
os.environ.setdefault("EVENT_BUS_NAME", "realestate-video-pipeline")
os.environ.setdefault("INPUT_BUCKET", "realestate-video-input")
os.environ.setdefault("KLING_SECRET_ID", "kling/api_key")
os.environ.setdefault("KLING_API_URL", "https://api.kling.ai/v1")
os.environ.setdefault("WEBHOOK_URL", "https://example.com/webhook/kling")

import importlib.util
import json
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

import shared.dynamo as dynamo

_spec = importlib.util.spec_from_file_location(
    "video_generation.handler",
    os.path.join(os.path.dirname(__file__), "..", "..", "src", "video_generation", "handler.py"),
)
vg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vg)

from shared.models import JobRecord, SceneSegment, StorySequence


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_JOB_ID = "a1b2c3d4-e5f6-4789-abcd-ef0123456789"
STORY_ID = "s1b2c3d4-e5f6-4789-abcd-ef0123456789"
INPUT_BUCKET = "realestate-video-input"
FAKE_API_KEY = "test-kling-api-key"
FAKE_TASK_ID_PREFIX = "kling-task-"


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
    """Spin up moto-backed DynamoDB, S3, and Secrets Manager."""
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name="us-east-1")

        ddb.create_table(
            TableName="property-video-jobs",
            KeySchema=[{"AttributeName": "job_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "job_id", "AttributeType": "S"}],
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

        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=INPUT_BUCKET)

        sm = boto3.client("secretsmanager", region_name="us-east-1")
        sm.create_secret(
            Name="kling/api_key",
            SecretString=json.dumps({"api_key": FAKE_API_KEY}),
        )

        dynamo._dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        vg._s3_client = boto3.client("s3", region_name="us-east-1")

        from shared.secrets import SecretsManagerClient
        vg._secrets_client = SecretsManagerClient(region_name="us-east-1")

        yield {"ddb": ddb, "s3": s3, "sm": sm}

        vg._s3_client = None
        vg._secrets_client = None


def _make_segment(index: int, job_id: str = VALID_JOB_ID) -> SceneSegment:
    return SceneSegment(
        segment_index=index,
        image_id=f"img-{index:03d}",
        s3_key=f"property_photos/{job_id}/image_{index}.jpg",
        script_text=f"Scene {index} narration.",
        video_prompt=f"Cinematic shot of room {index}.",
        duration_seconds=5,
        camera_movement="slow_zoom_in",
    )


def _put_story(job_id: str = VALID_JOB_ID, segment_count: int = 2) -> StorySequence:
    segments = [_make_segment(i, job_id) for i in range(segment_count)]
    story = StorySequence(
        story_id=STORY_ID,
        job_id=job_id,
        full_script="Full narration script.",
        total_duration_seconds=70,
        segments=segments,
        created_at="2024-01-01T00:00:00+00:00",
    )
    dynamo.put_story_sequence(story)
    return story


def _put_job(job_id: str = VALID_JOB_ID) -> None:
    job = JobRecord(
        job_id=job_id,
        status="generating",
        created_at="2024-01-01T00:00:00+00:00",
        updated_at="2024-01-01T00:00:00+00:00",
        property_address="123 Main St",
        image_count=2,
    )
    dynamo.put_job(job)


def _make_event(job_id: str = VALID_JOB_ID) -> dict:
    return {"detail": {"job_id": job_id}}


def _mock_kling_success(task_id: str = "kling-task-001") -> MagicMock:
    mock_resp = MagicMock()
    mock_resp.status_code = 202
    mock_resp.json.return_value = {"task_id": task_id}
    return mock_resp


def _mock_kling_failure(status_code: int = 429, text: str = "Too Many Requests") -> MagicMock:
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.text = text
    return mock_resp


# ---------------------------------------------------------------------------
# Test: each segment receives a kling_task_id in DynamoDB after submission
# ---------------------------------------------------------------------------

def test_each_segment_receives_kling_task_id(aws_resources):
    _put_job()
    _put_story(segment_count=2)

    call_count = 0

    def side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return _mock_kling_success(task_id=f"kling-task-{call_count:03d}")

    with patch("requests.post", side_effect=side_effect):
        with patch.object(vg, "generate_presigned_url", return_value="https://presigned.example.com/img.jpg"):
            result = vg.handler(_make_event(), None)

    assert result == {"statusCode": 200}

    segments = dynamo.query_segments_by_job(VALID_JOB_ID)
    assert len(segments) == 2
    task_ids = {s["kling_task_id"] for s in segments}
    assert "kling-task-001" in task_ids
    assert "kling-task-002" in task_ids
    for seg in segments:
        assert seg["kling_status"] == "queued"


# ---------------------------------------------------------------------------
# Test: presigned URL expiry outside [3600, 604800] raises ValueError
# ---------------------------------------------------------------------------

def test_presigned_url_expiry_too_low_raises_value_error(aws_resources):
    with pytest.raises(ValueError, match="expires must be between"):
        vg.generate_presigned_url("some/key.jpg", expires=3599)


def test_presigned_url_expiry_too_high_raises_value_error(aws_resources):
    with pytest.raises(ValueError, match="expires must be between"):
        vg.generate_presigned_url("some/key.jpg", expires=604801)


def test_presigned_url_expiry_at_lower_bound_succeeds(aws_resources):
    s3 = aws_resources["s3"]
    s3.put_object(Bucket=INPUT_BUCKET, Key="test/image.jpg", Body=b"fake")
    url = vg.generate_presigned_url("test/image.jpg", expires=3600)
    assert url.startswith("https://")


def test_presigned_url_expiry_at_upper_bound_succeeds(aws_resources):
    s3 = aws_resources["s3"]
    s3.put_object(Bucket=INPUT_BUCKET, Key="test/image.jpg", Body=b"fake")
    url = vg.generate_presigned_url("test/image.jpg", expires=604800)
    assert url.startswith("https://")


# ---------------------------------------------------------------------------
# Test: Kling.ai non-202 response raises VideoGenerationError
# ---------------------------------------------------------------------------

def test_non_202_kling_response_raises_video_generation_error():
    with patch("requests.post", return_value=_mock_kling_failure(status_code=500, text="Internal Server Error")):
        with pytest.raises(vg.VideoGenerationError) as exc_info:
            vg.submit_video_task(
                image_url="https://presigned.example.com/img.jpg",
                prompt="Cinematic shot",
                job_id=VALID_JOB_ID,
                segment_index=0,
                camera_movement="slow_zoom_in",
                duration_seconds=5,
                api_key=FAKE_API_KEY,
            )

    assert exc_info.value.status_code == 500
    assert "Internal Server Error" in exc_info.value.message


def test_non_202_kling_response_halts_processing(aws_resources):
    _put_job()
    _put_story(segment_count=2)

    with patch("requests.post", return_value=_mock_kling_failure(status_code=429)):
        with patch.object(vg, "generate_presigned_url", return_value="https://presigned.example.com/img.jpg"):
            with pytest.raises(vg.VideoGenerationError):
                vg.handler(_make_event(), None)

    # No segments should have task IDs since the first call failed
    segments = dynamo.query_segments_by_job(VALID_JOB_ID)
    for seg in segments:
        assert seg.get("kling_task_id") is None


# ---------------------------------------------------------------------------
# Test: build_kling_payload returns all required fields
# ---------------------------------------------------------------------------

def test_build_kling_payload_returns_all_required_fields():
    payload = vg.build_kling_payload(
        image_url="https://presigned.example.com/img.jpg",
        prompt="Slow cinematic zoom",
        duration_seconds=5,
        camera_movement="slow_zoom_in",
        webhook_url="https://example.com/webhook/kling",
    )

    assert payload["image_url"] == "https://presigned.example.com/img.jpg"
    assert payload["prompt"] == "Slow cinematic zoom"
    assert payload["duration"] == 5
    assert payload["aspect_ratio"] == "16:9"
    assert payload["resolution"] == "1080p"
    assert payload["mode"] == "cinematic"
    assert payload["camera_movement"] == "slow_zoom_in"
    assert payload["webhook_url"] == "https://example.com/webhook/kling"


# ---------------------------------------------------------------------------
# Test: submit_video_task sends correct Authorization header
# ---------------------------------------------------------------------------

def test_submit_video_task_sends_correct_authorization_header():
    with patch("requests.post", return_value=_mock_kling_success()) as mock_post:
        vg.submit_video_task(
            image_url="https://presigned.example.com/img.jpg",
            prompt="Cinematic shot",
            job_id=VALID_JOB_ID,
            segment_index=0,
            camera_movement="slow_zoom_in",
            duration_seconds=5,
            api_key=FAKE_API_KEY,
        )

    mock_post.assert_called_once()
    headers = mock_post.call_args[1]["headers"]
    assert headers["Authorization"] == f"Bearer {FAKE_API_KEY}"
    assert headers["Content-Type"] == "application/json"


def test_submit_video_task_returns_task_id():
    with patch("requests.post", return_value=_mock_kling_success(task_id="task-xyz-123")):
        task_id = vg.submit_video_task(
            image_url="https://presigned.example.com/img.jpg",
            prompt="Cinematic shot",
            job_id=VALID_JOB_ID,
            segment_index=0,
            camera_movement="slow_zoom_in",
            duration_seconds=5,
            api_key=FAKE_API_KEY,
        )

    assert task_id == "task-xyz-123"
