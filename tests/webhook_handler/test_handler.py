"""Unit tests for KlingWebhookHandlerFunction using moto and unittest.mock."""
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
os.environ.setdefault("KLING_WEBHOOK_SECRET_ID", "kling/webhook_secret")

import hashlib
import hmac
import importlib.util
import json
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

import shared.dynamo as dynamo

_spec = importlib.util.spec_from_file_location(
    "webhook_handler.handler",
    os.path.join(os.path.dirname(__file__), "..", "..", "src", "webhook_handler", "handler.py"),
)
wh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wh)

from shared.models import JobRecord, SceneSegment, StorySequence

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_JOB_ID = "a1b2c3d4-e5f6-4789-abcd-ef0123456789"
STORY_ID = "s1b2c3d4-e5f6-4789-abcd-ef0123456789"
ASSETS_BUCKET = "realestate-video-assets"
FAKE_SECRET = "super-secret-hmac-key"
FAKE_TASK_ID = "kling-task-001"
FAKE_VIDEO_URL = "https://cdn.kling.ai/videos/segment-001.mp4"
FAKE_VIDEO_BYTES = b"fake-video-content"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_signature(body: str, secret: str = FAKE_SECRET) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        body.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _make_completed_payload(task_id: str = FAKE_TASK_ID, video_url: str = FAKE_VIDEO_URL) -> dict:
    return {"task_id": task_id, "status": "completed", "video_url": video_url}


def _make_failed_payload(task_id: str = FAKE_TASK_ID, error_message: str = "generation failed") -> dict:
    return {"task_id": task_id, "status": "failed", "error_message": error_message}


def _make_event(payload: dict, secret: str = FAKE_SECRET, override_sig: str | None = None) -> dict:
    body = json.dumps(payload)
    sig = override_sig if override_sig is not None else _make_signature(body, secret)
    return {
        "body": body,
        "headers": {"X-Kling-Signature": sig},
        "requestContext": {"identity": {"sourceIp": "1.2.3.4"}},
    }


def _make_segment(index: int, task_id: str | None = None, kling_status: str = "queued") -> SceneSegment:
    seg = SceneSegment(
        segment_index=index,
        image_id=f"img-{index:03d}",
        s3_key=f"property_photos/{VALID_JOB_ID}/image_{index}.jpg",
        script_text=f"Scene {index} narration.",
        video_prompt=f"Cinematic shot of room {index}.",
        duration_seconds=5,
        camera_movement="slow_zoom_in",
        kling_task_id=task_id,
        kling_status=kling_status,
    )
    return seg


def _put_story(segments: list[SceneSegment]) -> StorySequence:
    story = StorySequence(
        story_id=STORY_ID,
        job_id=VALID_JOB_ID,
        full_script="Full narration script.",
        total_duration_seconds=70,
        segments=segments,
        created_at="2024-01-01T00:00:00+00:00",
    )
    dynamo.put_story_sequence(story)
    # Set top-level kling_task_id attributes so the GSI can find segments.
    # update_segment_kling_fields sets the top-level kling_task_id for each segment.
    for seg in segments:
        if seg.kling_task_id:
            dynamo.update_segment_kling_fields(
                story_id=STORY_ID,
                segment_index=seg.segment_index,
                kling_task_id=seg.kling_task_id,
                kling_status=seg.kling_status or "queued",
            )
    return story


def _put_job() -> None:
    job = JobRecord(
        job_id=VALID_JOB_ID,
        status="generating",
        created_at="2024-01-01T00:00:00+00:00",
        updated_at="2024-01-01T00:00:00+00:00",
        property_address="123 Main St",
        image_count=2,
    )
    dynamo.put_job(job)


def _mock_requests_get(content: bytes = FAKE_VIDEO_BYTES) -> MagicMock:
    mock_resp = MagicMock()
    mock_resp.content = content
    mock_resp.raise_for_status = MagicMock()
    return mock_resp


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
    """Spin up moto-backed DynamoDB, S3, Secrets Manager, and EventBridge."""
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

        sm = boto3.client("secretsmanager", region_name="us-east-1")
        sm.create_secret(
            Name="kling/webhook_secret",
            SecretString=json.dumps({"webhook_secret": FAKE_SECRET}),
        )

        eb = boto3.client("events", region_name="us-east-1")
        eb.create_event_bus(Name="realestate-video-pipeline")

        dynamo._dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        wh._s3_client = boto3.client("s3", region_name="us-east-1")
        wh._eb_client = boto3.client("events", region_name="us-east-1")

        from shared.secrets import SecretsManagerClient
        wh._secrets_client = SecretsManagerClient(region_name="us-east-1")

        yield {"ddb": ddb, "s3": s3, "sm": sm, "eb": eb}

        wh._s3_client = None
        wh._eb_client = None
        wh._secrets_client = None


# ---------------------------------------------------------------------------
# Test: valid HMAC + status=completed stores segment in S3 and updates DynamoDB
# ---------------------------------------------------------------------------

def test_completed_webhook_stores_video_in_s3_and_updates_dynamo(aws_resources):
    _put_job()
    seg0 = _make_segment(0, task_id=FAKE_TASK_ID)
    seg1 = _make_segment(1, task_id="kling-task-002", kling_status="complete")
    _put_story([seg0, seg1])

    payload = _make_completed_payload(task_id=FAKE_TASK_ID)
    event = _make_event(payload)

    with patch("requests.get", return_value=_mock_requests_get()):
        result = wh.handler(event, None)

    assert result["statusCode"] == 200

    # Verify S3 upload
    s3 = aws_resources["s3"]
    obj = s3.get_object(Bucket=ASSETS_BUCKET, Key=f"segments/{VALID_JOB_ID}/0.mp4")
    assert obj["Body"].read() == FAKE_VIDEO_BYTES

    # Verify DynamoDB update
    segments = dynamo.query_segments_by_job(VALID_JOB_ID)
    seg = next(s for s in segments if s.get("segment_index") == 0)
    assert seg["kling_status"] == "complete"
    assert seg["video_s3_key"] == f"segments/{VALID_JOB_ID}/0.mp4"


# ---------------------------------------------------------------------------
# Test: invalid HMAC returns 401 with no DynamoDB mutations
# ---------------------------------------------------------------------------

def test_invalid_hmac_returns_401_no_dynamo_mutations(aws_resources):
    _put_job()
    seg0 = _make_segment(0, task_id=FAKE_TASK_ID)
    _put_story([seg0])

    payload = _make_completed_payload()
    event = _make_event(payload, override_sig="bad-signature")

    result = wh.handler(event, None)

    assert result["statusCode"] == 401
    assert "Invalid signature" in result["body"]

    # DynamoDB should be unchanged
    segments = dynamo.query_segments_by_job(VALID_JOB_ID)
    seg = next(s for s in segments if s.get("segment_index") == 0)
    assert seg.get("kling_status") == "queued"


def test_missing_signature_returns_401(aws_resources):
    _put_job()
    seg0 = _make_segment(0, task_id=FAKE_TASK_ID)
    _put_story([seg0])

    payload = _make_completed_payload()
    body = json.dumps(payload)
    event = {"body": body, "headers": {}, "requestContext": {"identity": {"sourceIp": "1.2.3.4"}}}

    result = wh.handler(event, None)

    assert result["statusCode"] == 401


# ---------------------------------------------------------------------------
# Test: status=failed sets kling_status=failed and records error_message
# ---------------------------------------------------------------------------

def test_failed_webhook_sets_kling_status_failed(aws_resources):
    _put_job()
    seg0 = _make_segment(0, task_id=FAKE_TASK_ID)
    _put_story([seg0])

    payload = _make_failed_payload(error_message="GPU timeout")
    event = _make_event(payload)

    result = wh.handler(event, None)

    assert result["statusCode"] == 200

    # Query raw story item to check error_message (SceneSegment model doesn't include it)
    import json
    ddb_resource = boto3.resource("dynamodb", region_name="us-east-1")
    table = ddb_resource.Table("property-video-stories")
    raw = table.get_item(Key={"story_id": STORY_ID})["Item"]
    segs = json.loads(raw["segments"]) if isinstance(raw["segments"], str) else raw["segments"]
    seg = next(s for s in segs if s.get("segment_index") == 0)
    assert seg["kling_status"] == "failed"
    assert seg.get("error_message") == "GPU timeout"


# ---------------------------------------------------------------------------
# Test: idempotent re-processing produces no duplicate events
# ---------------------------------------------------------------------------

def test_idempotent_reprocessing_skips_duplicate(aws_resources):
    _put_job()
    # Segment already marked complete
    seg0 = _make_segment(0, task_id=FAKE_TASK_ID, kling_status="complete")
    seg1 = _make_segment(1, task_id="kling-task-002", kling_status="complete")
    _put_story([seg0, seg1])

    payload = _make_completed_payload(task_id=FAKE_TASK_ID)
    event = _make_event(payload)

    with patch.object(wh, "emit_all_segments_complete") as mock_emit:
        with patch("requests.get", return_value=_mock_requests_get()):
            result = wh.handler(event, None)

    assert result["statusCode"] == 200
    # Should NOT emit again since segment was already complete
    mock_emit.assert_not_called()


# ---------------------------------------------------------------------------
# Test: unknown task_id returns 400 with no DynamoDB mutations
# ---------------------------------------------------------------------------

def test_unknown_task_id_returns_400_no_mutations(aws_resources):
    _put_job()
    seg0 = _make_segment(0, task_id=FAKE_TASK_ID)
    _put_story([seg0])

    payload = _make_completed_payload(task_id="unknown-task-xyz")
    event = _make_event(payload)

    result = wh.handler(event, None)

    assert result["statusCode"] == 400
    assert "Unknown task_id" in result["body"]

    # DynamoDB unchanged
    segments = dynamo.query_segments_by_job(VALID_JOB_ID)
    seg = next(s for s in segments if s.get("segment_index") == 0)
    assert seg.get("kling_status") == "queued"


# ---------------------------------------------------------------------------
# Test: check_all_segments_complete returns True when all complete
# ---------------------------------------------------------------------------

def test_check_all_segments_complete_returns_true_when_all_done(aws_resources):
    _put_job()
    seg0 = _make_segment(0, task_id=FAKE_TASK_ID, kling_status="complete")
    seg1 = _make_segment(1, task_id="kling-task-002", kling_status="complete")
    _put_story([seg0, seg1])

    assert wh.check_all_segments_complete(VALID_JOB_ID) is True


# ---------------------------------------------------------------------------
# Test: check_all_segments_complete returns False when any pending
# ---------------------------------------------------------------------------

def test_check_all_segments_complete_returns_false_when_any_pending(aws_resources):
    _put_job()
    seg0 = _make_segment(0, task_id=FAKE_TASK_ID, kling_status="complete")
    seg1 = _make_segment(1, task_id="kling-task-002", kling_status="queued")
    _put_story([seg0, seg1])

    assert wh.check_all_segments_complete(VALID_JOB_ID) is False


def test_check_all_segments_complete_returns_false_when_any_failed(aws_resources):
    _put_job()
    seg0 = _make_segment(0, task_id=FAKE_TASK_ID, kling_status="complete")
    seg1 = _make_segment(1, task_id="kling-task-002", kling_status="failed")
    _put_story([seg0, seg1])

    assert wh.check_all_segments_complete(VALID_JOB_ID) is False


# ---------------------------------------------------------------------------
# Test: validate_webhook_signature with valid and invalid signatures
# ---------------------------------------------------------------------------

def test_validate_webhook_signature_valid():
    body = '{"task_id": "abc", "status": "completed"}'
    sig = _make_signature(body)
    assert wh.validate_webhook_signature(body, sig, FAKE_SECRET) is True


def test_validate_webhook_signature_invalid():
    body = '{"task_id": "abc", "status": "completed"}'
    assert wh.validate_webhook_signature(body, "wrong-sig", FAKE_SECRET) is False


def test_validate_webhook_signature_empty_signature():
    body = '{"task_id": "abc", "status": "completed"}'
    assert wh.validate_webhook_signature(body, "", FAKE_SECRET) is False


def test_validate_webhook_signature_none_signature():
    body = '{"task_id": "abc", "status": "completed"}'
    assert wh.validate_webhook_signature(body, None, FAKE_SECRET) is False


def test_validate_webhook_signature_tampered_body():
    body = '{"task_id": "abc", "status": "completed"}'
    sig = _make_signature(body)
    tampered = '{"task_id": "abc", "status": "failed"}'
    assert wh.validate_webhook_signature(tampered, sig, FAKE_SECRET) is False


# ---------------------------------------------------------------------------
# Test: all-segments-complete emitted when last segment completes
# ---------------------------------------------------------------------------

def test_all_segments_complete_event_emitted_when_last_segment_done(aws_resources):
    _put_job()
    seg0 = _make_segment(0, task_id=FAKE_TASK_ID, kling_status="queued")
    seg1 = _make_segment(1, task_id="kling-task-002", kling_status="complete")
    _put_story([seg0, seg1])

    payload = _make_completed_payload(task_id=FAKE_TASK_ID)
    event = _make_event(payload)

    with patch.object(wh, "emit_all_segments_complete") as mock_emit:
        with patch("requests.get", return_value=_mock_requests_get()):
            result = wh.handler(event, None)

    assert result["statusCode"] == 200
    mock_emit.assert_called_once_with(VALID_JOB_ID)


def test_all_segments_complete_not_emitted_when_segments_pending(aws_resources):
    _put_job()
    seg0 = _make_segment(0, task_id=FAKE_TASK_ID, kling_status="queued")
    seg1 = _make_segment(1, task_id="kling-task-002", kling_status="queued")
    _put_story([seg0, seg1])

    payload = _make_completed_payload(task_id=FAKE_TASK_ID)
    event = _make_event(payload)

    with patch.object(wh, "emit_all_segments_complete") as mock_emit:
        with patch("requests.get", return_value=_mock_requests_get()):
            result = wh.handler(event, None)

    assert result["statusCode"] == 200
    mock_emit.assert_not_called()
