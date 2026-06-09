import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src")))
os.environ.setdefault("JOBS_TABLE", "property-video-jobs")
os.environ.setdefault("IMAGES_TABLE", "property-video-images")
os.environ.setdefault("STORIES_TABLE", "property-video-stories")

import pytest
import boto3
from moto import mock_aws
from botocore.exceptions import ClientError

from shared.models import ImageAnalysisResult, JobRecord
import shared.dynamo as dynamo


@pytest.fixture(autouse=True)
def aws_credentials(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@pytest.fixture()
def dynamodb_tables():
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="property-video-jobs",
            KeySchema=[{"AttributeName": "job_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "job_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        client.create_table(
            TableName="property-video-images",
            KeySchema=[{"AttributeName": "image_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "image_id", "AttributeType": "S"},
                {"AttributeName": "job_id", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[{
                "IndexName": "job_id-index",
                "KeySchema": [{"AttributeName": "job_id", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            }],
            BillingMode="PAY_PER_REQUEST",
        )
        client.create_table(
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
        dynamo._dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        yield


def _make_job(job_id="job-1", image_count=3):
    return JobRecord(
        job_id=job_id,
        status="analyzing",
        created_at="2024-01-01T00:00:00+00:00",
        updated_at="2024-01-01T00:00:00+00:00",
        property_address="123 Main St",
        image_count=image_count,
    )


def _make_image(image_id="img-1", job_id="job-1"):
    return ImageAnalysisResult(
        image_id=image_id,
        job_id=job_id,
        s3_key=f"property_photos/{job_id}/{image_id}.jpg",
        sequence_index=0,
        room_type="living_room",
        architectural_style="modern",
        key_selling_points=["natural light"],
        lighting_quality="excellent",
        ambiance="warm",
        composition_score=0.85,
        analysis_timestamp="2024-01-01T00:00:00+00:00",
    )


def test_put_and_get_job_round_trip(dynamodb_tables):
    job = _make_job()
    dynamo.put_job(job)
    retrieved = dynamo.get_job(job.job_id)
    assert retrieved is not None
    assert retrieved.job_id == job.job_id
    assert retrieved.status == job.status
    assert retrieved.image_count == job.image_count
    assert retrieved.property_address == job.property_address


def test_get_job_returns_none_for_missing(dynamodb_tables):
    assert dynamo.get_job("nonexistent-id") is None


@pytest.mark.parametrize("image_count", [0, -1, 21, 100])
def test_put_job_rejects_invalid_image_count(dynamodb_tables, image_count):
    job = _make_job(image_count=image_count)
    with pytest.raises(ValueError, match="image_count"):
        dynamo.put_job(job)


@pytest.mark.parametrize("image_count", [1, 10, 20])
def test_put_job_accepts_valid_image_count(dynamodb_tables, image_count):
    job = _make_job(image_count=image_count)
    dynamo.put_job(job)
    retrieved = dynamo.get_job(job.job_id)
    assert retrieved.image_count == image_count


def test_increment_images_analyzed_returns_updated_value(dynamodb_tables):
    job = _make_job(image_count=3)
    dynamo.put_job(job)
    result = dynamo.increment_images_analyzed(job.job_id)
    assert int(result["images_analyzed"]) == 1
    result = dynamo.increment_images_analyzed(job.job_id)
    assert int(result["images_analyzed"]) == 2
    result = dynamo.increment_images_analyzed(job.job_id)
    assert int(result["images_analyzed"]) == 3


def test_increment_images_analyzed_returns_all_fields(dynamodb_tables):
    job = _make_job()
    dynamo.put_job(job)
    result = dynamo.increment_images_analyzed(job.job_id)
    assert "job_id" in result
    assert "status" in result


def test_query_images_by_job_returns_all_records(dynamodb_tables):
    job_id = "job-abc"
    for i in range(4):
        dynamo.put_image_result(_make_image(image_id=f"img-{i}", job_id=job_id))
    dynamo.put_image_result(_make_image(image_id="img-other", job_id="other-job"))
    results = dynamo.query_images_by_job(job_id)
    assert len(results) == 4
    assert {r.image_id for r in results} == {f"img-{i}" for i in range(4)}


def test_query_images_by_job_returns_empty_for_unknown_job(dynamodb_tables):
    assert dynamo.query_images_by_job("no-such-job") == []


def test_safe_update_job_status_succeeds_on_valid_forward_transition(dynamodb_tables):
    job = _make_job()
    dynamo.put_job(job)
    dynamo.safe_update_job_status(job.job_id, "analyzing", "sequencing")
    updated = dynamo.get_job(job.job_id)
    assert updated.status == "sequencing"


def test_safe_update_job_status_raises_on_backward_transition(dynamodb_tables):
    job = _make_job()
    dynamo.put_job(job)
    dynamo.update_job_status(job.job_id, "sequencing")
    with pytest.raises(ClientError) as exc_info:
        dynamo.safe_update_job_status(job.job_id, "analyzing", "sequencing")
    assert exc_info.value.response["Error"]["Code"] == "ConditionalCheckFailedException"


def test_safe_update_job_status_raises_when_status_already_advanced(dynamodb_tables):
    job = _make_job()
    dynamo.put_job(job)
    dynamo.update_job_status(job.job_id, "voiceover")
    with pytest.raises(ClientError) as exc_info:
        dynamo.safe_update_job_status(job.job_id, "analyzing", "sequencing")
    assert exc_info.value.response["Error"]["Code"] == "ConditionalCheckFailedException"


@pytest.mark.parametrize("current_status", ["analyzing", "sequencing", "voiceover", "generating", "assembling"])
def test_update_job_status_to_failed_succeeds_from_any_state(dynamodb_tables, current_status):
    """update_job_status to failed is unconditional and works from any state."""
    job = _make_job()
    dynamo.put_job(job)
    dynamo.update_job_status(job.job_id, current_status)

    # Should not raise - no conditional check
    dynamo.update_job_status(job.job_id, "failed")

    updated = dynamo.get_job(job.job_id)
    assert updated.status == "failed"
