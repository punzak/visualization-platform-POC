"""Unit tests for ImageAnalysisFunction using moto."""
from __future__ import annotations

# ruff: noqa
# fmt: off
import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src")))
os.environ.setdefault("JOBS_TABLE", "property-video-jobs")
os.environ.setdefault("IMAGES_TABLE", "property-video-images")
os.environ.setdefault("STORIES_TABLE", "property-video-stories")
os.environ.setdefault("INPUT_BUCKET", "realestate-video-input")
os.environ.setdefault("EVENT_BUS_NAME", "realestate-video-pipeline")
os.environ.setdefault("BEDROCK_REGION", "us-east-1")

import json
import uuid
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

import shared.dynamo as dynamo
import importlib, importlib.util
_spec = importlib.util.spec_from_file_location(
    "image_analysis.handler",
    os.path.join(os.path.dirname(__file__), "..", "..", "src", "image_analysis", "handler.py"),
)
ia = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ia)
from shared.models import JobRecord


# ---------------------------------------------------------------------------
# Helpers / constants
# ---------------------------------------------------------------------------

VALID_JOB_ID = "a1b2c3d4-e5f6-4789-abcd-ef0123456789"
INPUT_BUCKET = "realestate-video-input"
EVENT_BUS = "realestate-video-pipeline"

# Minimal valid JPEG magic bytes + padding to stay under 10 MB
JPEG_BYTES = b"\xff\xd8\xff" + b"\x00" * 100
PNG_BYTES = b"\x89PNG" + b"\x00" * 100
WEBP_BYTES = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 100
GIF_BYTES = b"GIF89a" + b"\x00" * 100

VALID_ANALYSIS_JSON = {
    "room_type": "living_room",
    "architectural_style": "modern",
    "key_selling_points": ["natural light", "open plan"],
    "lighting_quality": "excellent",
    "ambiance": "warm",
    "composition_score": 0.85,
}

BEDROCK_RESPONSE_BODY = {
    "content": [{"type": "text", "text": json.dumps(VALID_ANALYSIS_JSON)}]
}


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
    """Spin up moto-backed S3, DynamoDB, and EventBridge resources."""
    with mock_aws():
        # S3
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=INPUT_BUCKET)

        # DynamoDB
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

        # EventBridge custom bus
        events = boto3.client("events", region_name="us-east-1")
        events.create_event_bus(Name=EVENT_BUS)

        # Wire moto resources into module singletons
        dynamo._dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        ia._s3_client = boto3.client("s3", region_name="us-east-1")
        ia._events_client = boto3.client("events", region_name="us-east-1")

        yield {"s3": s3, "ddb": ddb, "events": events}

        # Reset singletons after each test
        ia._s3_client = None
        ia._bedrock_client = None
        ia._events_client = None


def _put_job(job_id: str, image_count: int = 1) -> None:
    job = JobRecord(
        job_id=job_id,
        status="analyzing",
        created_at="2024-01-01T00:00:00+00:00",
        updated_at="2024-01-01T00:00:00+00:00",
        property_address="123 Main St",
        image_count=image_count,
    )
    dynamo.put_job(job)


def _upload_image(s3_client, job_id: str, filename: str, image_bytes: bytes) -> str:
    key = f"property_photos/{job_id}/{filename}"
    s3_client.put_object(Bucket=INPUT_BUCKET, Key=key, Body=image_bytes)
    return key


def _make_s3_event(bucket: str, key: str) -> dict:
    return {
        "Records": [
            {
                "messageId": "msg-001",
                "s3": {
                    "bucket": {"name": bucket},
                    "object": {"key": key},
                },
            }
        ]
    }


def _mock_bedrock_client():
    """Return a mock Bedrock client that returns a valid analysis JSON."""
    mock_body = MagicMock()
    mock_body.read.return_value = json.dumps(BEDROCK_RESPONSE_BODY).encode()
    mock_client = MagicMock()
    mock_client.invoke_model.return_value = {"body": mock_body}
    return mock_client


# ---------------------------------------------------------------------------
# Test: valid S3 event triggers Bedrock invocation and DynamoDB write
# ---------------------------------------------------------------------------

def test_valid_event_triggers_bedrock_and_dynamo_write(aws_resources):
    job_id = VALID_JOB_ID
    _put_job(job_id, image_count=1)
    key = _upload_image(aws_resources["s3"], job_id, "living_room.jpg", JPEG_BYTES)

    mock_bedrock = _mock_bedrock_client()
    ia._bedrock_client = mock_bedrock

    event = _make_s3_event(INPUT_BUCKET, key)
    result = ia.handler(event, None)

    # Bedrock was called once
    mock_bedrock.invoke_model.assert_called_once()

    # No failures
    assert result["batchItemFailures"] == []

    # DynamoDB has the image record
    images = dynamo.query_images_by_job(job_id)
    assert len(images) == 1
    assert images[0].job_id == job_id
    assert images[0].room_type == "living_room"
    assert images[0].composition_score == 0.85


# ---------------------------------------------------------------------------
# Test: oversized image halts processing without calling Bedrock
# ---------------------------------------------------------------------------

def test_oversized_image_halts_without_bedrock(aws_resources):
    job_id = VALID_JOB_ID
    _put_job(job_id, image_count=1)

    oversized = JPEG_BYTES[:3] + b"\x00" * (10 * 1024 * 1024 + 1)
    key = _upload_image(aws_resources["s3"], job_id, "big.jpg", oversized)

    mock_bedrock = _mock_bedrock_client()
    ia._bedrock_client = mock_bedrock

    event = _make_s3_event(INPUT_BUCKET, key)
    result = ia.handler(event, None)

    # Bedrock must NOT be called
    mock_bedrock.invoke_model.assert_not_called()

    # The record should be in batchItemFailures
    assert len(result["batchItemFailures"]) == 1


# ---------------------------------------------------------------------------
# Test: unsupported image format halts processing
# ---------------------------------------------------------------------------

def test_unsupported_format_halts_processing(aws_resources):
    job_id = VALID_JOB_ID
    _put_job(job_id, image_count=1)
    key = _upload_image(aws_resources["s3"], job_id, "photo.gif", GIF_BYTES)

    mock_bedrock = _mock_bedrock_client()
    ia._bedrock_client = mock_bedrock

    event = _make_s3_event(INPUT_BUCKET, key)
    result = ia.handler(event, None)

    mock_bedrock.invoke_model.assert_not_called()
    assert len(result["batchItemFailures"]) == 1


# ---------------------------------------------------------------------------
# Test: batchItemFailures contains only failed record IDs
# ---------------------------------------------------------------------------

def test_batch_item_failures_contains_only_failed_records(aws_resources):
    job_id = VALID_JOB_ID
    _put_job(job_id, image_count=2)

    good_key = _upload_image(aws_resources["s3"], job_id, "good.jpg", JPEG_BYTES)
    bad_key = _upload_image(aws_resources["s3"], job_id, "bad.gif", GIF_BYTES)

    mock_bedrock = _mock_bedrock_client()
    ia._bedrock_client = mock_bedrock

    event = {
        "Records": [
            {"messageId": "msg-good", "s3": {"bucket": {"name": INPUT_BUCKET}, "object": {"key": good_key}}},
            {"messageId": "msg-bad",  "s3": {"bucket": {"name": INPUT_BUCKET}, "object": {"key": bad_key}}},
        ]
    }
    result = ia.handler(event, None)

    failed_ids = [f["itemIdentifier"] for f in result["batchItemFailures"]]
    assert "msg-bad" in failed_ids
    assert "msg-good" not in failed_ids
    assert len(failed_ids) == 1


# ---------------------------------------------------------------------------
# Test: emit_all_images_analyzed called when images_analyzed == image_count
# ---------------------------------------------------------------------------

def test_emit_all_images_analyzed_when_complete(aws_resources):
    job_id = VALID_JOB_ID
    _put_job(job_id, image_count=1)
    key = _upload_image(aws_resources["s3"], job_id, "room.jpg", JPEG_BYTES)

    mock_bedrock = _mock_bedrock_client()
    ia._bedrock_client = mock_bedrock

    with patch.object(ia, "emit_all_images_analyzed") as mock_emit:
        event = _make_s3_event(INPUT_BUCKET, key)
        ia.handler(event, None)
        mock_emit.assert_called_once_with(job_id, 1)


# ---------------------------------------------------------------------------
# Test: emit_all_images_analyzed NOT called when images_analyzed < image_count
# ---------------------------------------------------------------------------

def test_emit_not_called_when_not_all_images_analyzed(aws_resources):
    job_id = VALID_JOB_ID
    _put_job(job_id, image_count=3)  # 3 expected, only 1 uploaded
    key = _upload_image(aws_resources["s3"], job_id, "room.jpg", JPEG_BYTES)

    mock_bedrock = _mock_bedrock_client()
    ia._bedrock_client = mock_bedrock

    with patch.object(ia, "emit_all_images_analyzed") as mock_emit:
        event = _make_s3_event(INPUT_BUCKET, key)
        ia.handler(event, None)
        mock_emit.assert_not_called()


# ---------------------------------------------------------------------------
# Unit tests for check_image_format
# ---------------------------------------------------------------------------

def test_check_image_format_jpeg():
    assert ia.check_image_format(JPEG_BYTES) == "image/jpeg"


def test_check_image_format_png():
    assert ia.check_image_format(PNG_BYTES) == "image/png"


def test_check_image_format_webp():
    assert ia.check_image_format(WEBP_BYTES) == "image/webp"


def test_check_image_format_unsupported_raises():
    with pytest.raises(ValueError, match="Unsupported image format"):
        ia.check_image_format(GIF_BYTES)


# ---------------------------------------------------------------------------
# Unit tests for parse_analysis_response
# ---------------------------------------------------------------------------

def test_parse_analysis_response_valid():
    body = {"content": [{"type": "text", "text": json.dumps(VALID_ANALYSIS_JSON)}]}
    result = ia.parse_analysis_response(body)
    assert result["room_type"] == "living_room"
    assert result["composition_score"] == 0.85


def test_parse_analysis_response_strips_markdown_fences():
    text = "```json\n" + json.dumps(VALID_ANALYSIS_JSON) + "\n```"
    body = {"content": [{"type": "text", "text": text}]}
    result = ia.parse_analysis_response(body)
    assert result["composition_score"] == 0.85


def test_parse_analysis_response_no_text_raises():
    with pytest.raises(ValueError, match="No text content"):
        ia.parse_analysis_response({"content": []})
