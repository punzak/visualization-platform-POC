"""Unit tests for VoiceoverGenerationFunction using moto and unittest.mock."""
from __future__ import annotations

# ruff: noqa
# fmt: off
import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src")))
os.environ.setdefault("JOBS_TABLE", "property-video-jobs")
os.environ.setdefault("IMAGES_TABLE", "property-video-images")
os.environ.setdefault("STORIES_TABLE", "property-video-stories")
os.environ.setdefault("EVENT_BUS_NAME", "realestate-video-pipeline")
os.environ.setdefault("ASSETS_BUCKET", "realestate-video-assets")
os.environ.setdefault("ELEVENLABS_SECRET_ID", "elevenlabs/api_key")

import importlib.util
import json
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

import shared.dynamo as dynamo

_spec = importlib.util.spec_from_file_location(
    "voiceover_generation.handler",
    os.path.join(os.path.dirname(__file__), "..", "..", "src", "voiceover_generation", "handler.py"),
)
vg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vg)

from shared.models import JobRecord, SceneSegment, StorySequence


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_JOB_ID = "a1b2c3d4-e5f6-4789-abcd-ef0123456789"
STORY_ID = "s1b2c3d4-e5f6-4789-abcd-ef0123456789"
EVENT_BUS = "realestate-video-pipeline"
ASSETS_BUCKET = "realestate-video-assets"
FAKE_AUDIO = b"ID3\x00\x00\x00fake-mp3-bytes"
FAKE_API_KEY = "test-api-key"
FAKE_VOICE_ID = "test-voice-id"


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
    """Spin up moto-backed DynamoDB, S3, EventBridge, and Secrets Manager."""
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
        s3.create_bucket(Bucket=ASSETS_BUCKET)

        events = boto3.client("events", region_name="us-east-1")
        events.create_event_bus(Name=EVENT_BUS)

        sm = boto3.client("secretsmanager", region_name="us-east-1")
        sm.create_secret(
            Name="elevenlabs/api_key",
            SecretString=json.dumps({"api_key": FAKE_API_KEY, "voice_id": FAKE_VOICE_ID}),
        )

        dynamo._dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        vg._s3_client = boto3.client("s3", region_name="us-east-1")
        vg._events_client = boto3.client("events", region_name="us-east-1")

        # Wire a real SecretsManagerClient backed by moto
        from shared.secrets import SecretsManagerClient
        vg._secrets_client = SecretsManagerClient(region_name="us-east-1")

        yield {"ddb": ddb, "s3": s3, "events": events, "sm": sm}

        # Reset singletons
        vg._s3_client = None
        vg._events_client = None
        vg._secrets_client = None


def _put_job(job_id: str = VALID_JOB_ID, status: str = "voiceover") -> None:
    job = JobRecord(
        job_id=job_id,
        status=status,
        created_at="2024-01-01T00:00:00+00:00",
        updated_at="2024-01-01T00:00:00+00:00",
        property_address="123 Main St",
        image_count=1,
    )
    dynamo.put_job(job)


def _put_story(job_id: str = VALID_JOB_ID) -> None:
    segment = SceneSegment(
        segment_index=0,
        image_id="img-001",
        s3_key=f"property_photos/{job_id}/living_room.jpg",
        script_text="Welcome to this stunning property.",
        video_prompt="Slow cinematic zoom into the living room.",
        duration_seconds=10,
        camera_movement="slow_zoom_in",
    )
    story = StorySequence(
        story_id=STORY_ID,
        job_id=job_id,
        full_script="Welcome to this stunning property.",
        total_duration_seconds=70,
        segments=[segment],
        created_at="2024-01-01T00:00:00+00:00",
    )
    dynamo.put_story_sequence(story)


def _make_event(job_id: str = VALID_JOB_ID) -> dict:
    return {"detail": {"job_id": job_id}}


def _mock_requests_post_success(audio_bytes: bytes = FAKE_AUDIO) -> MagicMock:
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.content = audio_bytes
    return mock_response


def _mock_requests_post_failure(status_code: int = 429, text: str = "Too Many Requests") -> MagicMock:
    mock_response = MagicMock()
    mock_response.status_code = status_code
    mock_response.text = text
    return mock_response


# ---------------------------------------------------------------------------
# Test: successful ElevenLabs response uploads MP3 to correct S3 key
# ---------------------------------------------------------------------------

def test_successful_response_uploads_mp3_to_correct_s3_key(aws_resources):
    _put_job()
    _put_story()

    with patch("requests.post", return_value=_mock_requests_post_success()):
        result = vg.handler(_make_event(), None)

    assert result == {"statusCode": 200}

    s3 = aws_resources["s3"]
    expected_key = f"voiceovers/{VALID_JOB_ID}/narration.mp3"
    obj = s3.get_object(Bucket=ASSETS_BUCKET, Key=expected_key)
    assert obj["Body"].read() == FAKE_AUDIO
    assert obj["ContentType"] == "audio/mpeg"


# ---------------------------------------------------------------------------
# Test: non-200 ElevenLabs response sets job status to failed and returns 500
# ---------------------------------------------------------------------------

def test_non_200_elevenlabs_response_sets_job_failed(aws_resources):
    _put_job()
    _put_story()

    with patch("requests.post", return_value=_mock_requests_post_failure(status_code=401, text="Unauthorized")):
        result = vg.handler(_make_event(), None)

    assert result == {"statusCode": 500}

    job = dynamo.get_job(VALID_JOB_ID)
    assert job.status == "failed"


# ---------------------------------------------------------------------------
# Test: voiceover-complete event emitted with correct job_id and voiceover_s3_key
# ---------------------------------------------------------------------------

def test_voiceover_complete_event_emitted(aws_resources):
    _put_job()
    _put_story()

    with patch("requests.post", return_value=_mock_requests_post_success()):
        with patch.object(vg, "emit_voiceover_complete") as mock_emit:
            vg.handler(_make_event(), None)

    mock_emit.assert_called_once()
    call_args = mock_emit.call_args[0]
    assert call_args[0] == VALID_JOB_ID
    assert call_args[1] == f"voiceovers/{VALID_JOB_ID}/narration.mp3"


# ---------------------------------------------------------------------------
# Test: upload_audio stores bytes at correct S3 key with correct ContentType
# ---------------------------------------------------------------------------

def test_upload_audio_stores_bytes_at_correct_key(aws_resources):
    audio = b"fake-audio-content"
    key = vg.upload_audio(audio, VALID_JOB_ID)

    assert key == f"voiceovers/{VALID_JOB_ID}/narration.mp3"

    s3 = aws_resources["s3"]
    obj = s3.get_object(Bucket=ASSETS_BUCKET, Key=key)
    assert obj["Body"].read() == audio
    assert obj["ContentType"] == "audio/mpeg"


# ---------------------------------------------------------------------------
# Test: generate_voiceover raises VoiceoverError on non-200 response
# ---------------------------------------------------------------------------

def test_generate_voiceover_raises_voiceover_error_on_non_200():
    with patch("requests.post", return_value=_mock_requests_post_failure(status_code=500, text="Internal Server Error")):
        with pytest.raises(vg.VoiceoverError) as exc_info:
            vg.generate_voiceover("Hello world", FAKE_VOICE_ID, FAKE_API_KEY)

    assert exc_info.value.status_code == 500
    assert "Internal Server Error" in exc_info.value.message


# ---------------------------------------------------------------------------
# Test: generate_voiceover returns audio bytes on success
# ---------------------------------------------------------------------------

def test_generate_voiceover_returns_audio_bytes_on_success():
    with patch("requests.post", return_value=_mock_requests_post_success(FAKE_AUDIO)):
        result = vg.generate_voiceover("Hello world", FAKE_VOICE_ID, FAKE_API_KEY)

    assert result == FAKE_AUDIO


# ---------------------------------------------------------------------------
# Test: generate_voiceover sends correct headers and body
# ---------------------------------------------------------------------------

def test_generate_voiceover_sends_correct_request():
    mock_response = _mock_requests_post_success()
    with patch("requests.post", return_value=mock_response) as mock_post:
        vg.generate_voiceover("Test script", FAKE_VOICE_ID, FAKE_API_KEY)

    mock_post.assert_called_once()
    call_kwargs = mock_post.call_args
    assert FAKE_VOICE_ID in call_kwargs[1]["url"] or FAKE_VOICE_ID in str(call_kwargs)
    headers = call_kwargs[1]["headers"]
    assert headers["xi-api-key"] == FAKE_API_KEY
    assert headers["Content-Type"] == "application/json"
    body = call_kwargs[1]["json"]
    assert body["text"] == "Test script"
    assert body["model_id"] == "eleven_multilingual_v2"
    assert body["voice_settings"]["stability"] == 0.5
    assert body["voice_settings"]["similarity_boost"] == 0.75
    assert call_kwargs[1]["timeout"] == 90


# ---------------------------------------------------------------------------
# Test: job status updated to generating on success
# ---------------------------------------------------------------------------

def test_job_status_updated_to_generating_on_success(aws_resources):
    _put_job()
    _put_story()

    with patch("requests.post", return_value=_mock_requests_post_success()):
        vg.handler(_make_event(), None)

    job = dynamo.get_job(VALID_JOB_ID)
    assert job.status == "generating"
    assert job.voiceover_s3_key == f"voiceovers/{VALID_JOB_ID}/narration.mp3"
