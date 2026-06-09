"""Unit tests for the Step Functions assembly Lambda functions using moto."""
from __future__ import annotations

# ruff: noqa
# fmt: off
import os, sys, importlib, importlib.util
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src")))
os.environ.setdefault("JOBS_TABLE", "property-video-jobs")
os.environ.setdefault("IMAGES_TABLE", "property-video-images")
os.environ.setdefault("STORIES_TABLE", "property-video-stories")
os.environ.setdefault("ASSETS_BUCKET", "realestate-video-assets")
os.environ.setdefault("OUTPUT_BUCKET", "realestate-video-output")

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src"))

def _load(module_name: str, rel_path: str):
    spec = importlib.util.spec_from_file_location(module_name, os.path.join(_SRC, rel_path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

import json
import pytest
import boto3
from moto import mock_aws

import shared.dynamo as dynamo
from shared.models import JobRecord, SceneSegment, StorySequence


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
    with mock_aws():
        # DynamoDB tables
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

        # S3 buckets
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="realestate-video-assets")
        s3.create_bucket(Bucket="realestate-video-output")

        # Wire moto resources into the dynamo module
        dynamo._dynamodb = boto3.resource("dynamodb", region_name="us-east-1")

        yield


def _make_job(job_id: str = "job-1", status: str = "assembling", image_count: int = 2) -> JobRecord:
    return JobRecord(
        job_id=job_id,
        status=status,
        created_at="2024-01-01T00:00:00+00:00",
        updated_at="2024-01-01T00:00:00+00:00",
        property_address="123 Main St",
        image_count=image_count,
    )


def _make_story(job_id: str, segments: list[SceneSegment]) -> StorySequence:
    return StorySequence(
        story_id=f"story-{job_id}",
        job_id=job_id,
        full_script="A beautiful property.",
        total_duration_seconds=60,
        segments=segments,
        created_at="2024-01-01T00:00:00+00:00",
    )


def _make_segment(index: int, job_id: str, video_s3_key: str = "") -> SceneSegment:
    return SceneSegment(
        segment_index=index,
        image_id=f"img-{index}",
        s3_key=f"property_photos/{job_id}/img-{index}.jpg",
        script_text="Beautiful room.",
        video_prompt="Slow zoom in.",
        duration_seconds=5,
        camera_movement="slow_zoom_in",
        kling_task_id=f"task-{index}",
        kling_status="complete",
        video_s3_key=video_s3_key,
    )


# ---------------------------------------------------------------------------
# retrieve_segments tests
# ---------------------------------------------------------------------------

def test_retrieve_segments_returns_keys_in_segment_index_order(aws_resources):
    """retrieve_segments returns segments sorted by segment_index."""
    rs = _load("assembly.retrieve_segments", "assembly/retrieve_segments.py")

    job_id = "job-order"
    # Create segments out of order
    seg0 = _make_segment(0, job_id, video_s3_key="segments/job-order/0.mp4")
    seg2 = _make_segment(2, job_id, video_s3_key="segments/job-order/2.mp4")
    seg1 = _make_segment(1, job_id, video_s3_key="segments/job-order/1.mp4")

    story = _make_story(job_id, [seg2, seg0, seg1])  # intentionally out of order
    dynamo.put_story_sequence(story)

    result = rs.handler({"job_id": job_id}, None)

    assert result["job_id"] == job_id
    indices = [s["segment_index"] for s in result["segments"]]
    assert indices == sorted(indices), "Segments must be in ascending segment_index order"
    assert indices == [0, 1, 2]


def test_retrieve_segments_raises_value_error_when_segment_missing_video_s3_key(aws_resources):
    """retrieve_segments raises ValueError if any segment has no video_s3_key."""
    rs = _load("assembly.retrieve_segments", "assembly/retrieve_segments.py")

    job_id = "job-missing-key"
    seg0 = _make_segment(0, job_id, video_s3_key="segments/job-missing-key/0.mp4")
    seg1 = _make_segment(1, job_id, video_s3_key="")  # missing key

    story = _make_story(job_id, [seg0, seg1])
    dynamo.put_story_sequence(story)

    with pytest.raises(ValueError, match="video_s3_key"):
        rs.handler({"job_id": job_id}, None)


# ---------------------------------------------------------------------------
# assemble_video tests
# ---------------------------------------------------------------------------

def test_assemble_video_uploads_concatenated_bytes_to_correct_s3_key(aws_resources, monkeypatch):
    """assemble_video concatenates segment bytes and uploads to final/{job_id}/property_tour.mp4."""
    av_module = _load("assembly.assemble_video", "assembly/assemble_video.py")

    job_id = "job-assemble"
    s3 = boto3.client("s3", region_name="us-east-1")

    # Upload fake segment files to assets bucket
    s3.put_object(Bucket="realestate-video-assets", Key="segments/job-assemble/0.mp4", Body=b"chunk0")
    s3.put_object(Bucket="realestate-video-assets", Key="segments/job-assemble/1.mp4", Body=b"chunk1")

    # Patch the module-level s3 client to use moto
    av_module._s3 = boto3.client("s3", region_name="us-east-1")

    event = {
        "job_id": job_id,
        "segments": [
            {"segment_index": 0, "video_s3_key": "segments/job-assemble/0.mp4"},
            {"segment_index": 1, "video_s3_key": "segments/job-assemble/1.mp4"},
        ],
    }

    result = av_module.handler(event, None)

    assert result["job_id"] == job_id
    assert result["final_video_s3_key"] == f"final/{job_id}/property_tour.mp4"

    # Verify the object was uploaded with concatenated content
    obj = s3.get_object(Bucket="realestate-video-output", Key=f"final/{job_id}/property_tour.mp4")
    body = obj["Body"].read()
    assert body == b"chunk0chunk1"


# ---------------------------------------------------------------------------
# finalize_job tests
# ---------------------------------------------------------------------------

def test_finalize_job_sets_status_complete_and_final_video_s3_key(aws_resources):
    """finalize_job updates status to complete and sets final_video_s3_key on success."""
    fj = _load("assembly.finalize_job", "assembly/finalize_job.py")

    job_id = "job-finalize-ok"
    job = _make_job(job_id=job_id, status="assembling")
    dynamo.put_job(job)

    event = {
        "job_id": job_id,
        "final_video_s3_key": f"final/{job_id}/property_tour.mp4",
    }

    result = fj.handler(event, None)

    assert result == {"statusCode": 200}

    updated = dynamo.get_job(job_id)
    assert updated.status == "complete"
    assert updated.final_video_s3_key == f"final/{job_id}/property_tour.mp4"


def test_finalize_job_sets_status_failed_and_error_message_on_failure(aws_resources):
    """finalize_job updates status to failed and sets error_message when error is present."""
    fj = _load("assembly.finalize_job", "assembly/finalize_job.py")

    job_id = "job-finalize-fail"
    job = _make_job(job_id=job_id, status="assembling")
    dynamo.put_job(job)

    event = {
        "job_id": job_id,
        "error": "S3 download failed: NoSuchKey",
    }

    result = fj.handler(event, None)

    assert result == {"statusCode": 200}

    updated = dynamo.get_job(job_id)
    assert updated.status == "failed"
    assert updated.error_message == "S3 download failed: NoSuchKey"
