"""ImageAnalysisFunction — Stage 1 of the Kling AI Video POC pipeline.

Triggered by SQS events (which wrap S3 ObjectCreated notifications).
Downloads each image, invokes Bedrock for structured metadata extraction,
persists the result to DynamoDB, and emits an EventBridge event when all
images for a job are analyzed.
"""
from __future__ import annotations

import base64
import json
import os
import uuid
from typing import Any

import boto3

from shared.dynamo import increment_images_analyzed, put_image_result, safe_update_job_status
from shared.logger import StructuredLogger
from shared.models import ImageAnalysisResult
from shared.utils import now_iso, parse_s3_key
from shared.xray import begin_subsegment, end_subsegment, put_annotation

EVENT_BUS_NAME = os.environ.get("EVENT_BUS_NAME", "default")
BEDROCK_MODEL_ID = os.environ.get(
    "BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")
BEDROCK_REGION = os.environ.get("BEDROCK_REGION", "us-east-1")
INPUT_BUCKET = os.environ.get("INPUT_BUCKET", "realestate-video-input")
MAX_IMAGE_BYTES = 10 * 1024 * 1024

logger = StructuredLogger("image_analysis")

_s3_client = None
_bedrock_client = None
_events_client = None


def _s3():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


def _bedrock():
    global _bedrock_client
    if _bedrock_client is None:
        _bedrock_client = boto3.client(
            "bedrock-runtime", region_name=BEDROCK_REGION)
    return _bedrock_client


def _events():
    global _events_client
    if _events_client is None:
        _events_client = boto3.client("events")
    return _events_client


def handler(event: dict, context: Any) -> dict:
    """SQS trigger — each SQS record wraps an S3 event JSON in its body.
    Processes images concurrently for speed.
    """
    batch_item_failures = []
    tasks = []

    # Collect all image keys from all SQS records
    for sqs_record in event.get("Records", []):
        record_id = sqs_record.get("messageId", "unknown")
        try:
            s3_event = json.loads(sqs_record.get("body", "{}"))
        except Exception:
            batch_item_failures.append({"itemIdentifier": record_id})
            continue

        for record in s3_event.get("Records", []):
            bucket = record["s3"]["bucket"]["name"]
            key = record["s3"]["object"]["key"]
            import urllib.parse
            key = urllib.parse.unquote_plus(key)
            tasks.append((record_id, bucket, key))

    # Process all images concurrently
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=min(len(tasks), 5)) as ex:
        futures = {ex.submit(analyze_image, bucket, key): (record_id, key)
                   for record_id, bucket, key in tasks}
        for f in as_completed(futures):
            record_id, key = futures[f]
            try:
                f.result()
            except Exception as exc:
                logger.error(job_id="unknown", stage="image_analysis",
                             outcome="record_failed", s3_key=key, error=str(exc))
                batch_item_failures.append({"itemIdentifier": record_id})

    return {"batchItemFailures": batch_item_failures}


def analyze_image(bucket: str, s3_key: str) -> ImageAnalysisResult:
    job_id, filename = parse_s3_key(s3_key)
    logger.info(job_id=job_id, stage="image_analysis",
                outcome="started", s3_key=s3_key)

    response = _s3().get_object(Bucket=bucket, Key=s3_key)
    image_bytes: bytes = response["Body"].read()

    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise ValueError(f"Image exceeds 10 MB: {len(image_bytes)} bytes")

    media_type = check_image_format(image_bytes)
    image_b64 = base64.b64encode(image_bytes).decode("utf-8")
    request_body = build_bedrock_request(image_b64, media_type)

    begin_subsegment("bedrock-invoke-model")
    put_annotation("job_id", job_id)
    try:
        bedrock_response = _bedrock().invoke_model(
            modelId=BEDROCK_MODEL_ID,
            body=json.dumps(request_body),
            contentType="application/json",
            accept="application/json",
        )
    except Exception as exc:
        logger.error(job_id=job_id, stage="image_analysis",
                     outcome="bedrock_error", s3_key=s3_key, error=str(exc))
        raise
    finally:
        end_subsegment()

    response_body = json.loads(bedrock_response["body"].read())
    analysis_dict = parse_analysis_response(response_body)

    composition_score = float(analysis_dict.get("composition_score", 0.5))
    composition_score = max(0.0, min(1.0, composition_score))

    key_selling_points = analysis_dict.get(
        "key_selling_points", ["property feature"])
    if not key_selling_points:
        key_selling_points = ["property feature"]

    result = ImageAnalysisResult(
        image_id=str(uuid.uuid4()),
        job_id=job_id,
        s3_key=s3_key,
        sequence_index=0,
        room_type=analysis_dict.get("room_type", "unknown"),
        architectural_style=analysis_dict.get(
            "architectural_style", "unknown"),
        key_selling_points=key_selling_points,
        lighting_quality=analysis_dict.get("lighting_quality", "good"),
        ambiance=analysis_dict.get("ambiance", "neutral"),
        composition_score=composition_score,
        analysis_timestamp=now_iso(),
    )

    put_image_result(result)

    updated_job = increment_images_analyzed(job_id)
    images_analyzed = int(updated_job.get("images_analyzed", 0))
    image_count = int(updated_job.get("image_count", -1))

    logger.info(job_id=job_id, stage="image_analysis", outcome="image_analyzed",
                images_analyzed=images_analyzed, image_count=image_count)

    if image_count > 0 and images_analyzed >= image_count:
        emit_all_images_analyzed(job_id, image_count)
        safe_update_job_status(job_id, "analyzing", "sequencing")

    return result


def build_bedrock_request(image_b64: str, media_type: str) -> dict:
    prompt = (
        "You are a professional real estate photographer and property analyst. "
        "Analyze this property image and return a JSON object with exactly these fields:\n"
        "- room_type (string): e.g. 'living_room', 'kitchen', 'master_bedroom', 'bathroom', 'exterior'\n"
        "- architectural_style (string): e.g. 'modern', 'colonial', 'craftsman', 'contemporary'\n"
        "- key_selling_points (array of strings): at least 1 item\n"
        "- lighting_quality (string): one of 'excellent', 'good', 'fair', 'poor'\n"
        "- ambiance (string): e.g. 'warm', 'bright', 'cozy', 'airy', 'elegant'\n"
        "- composition_score (number): float between 0.0 and 1.0\n\n"
        "Return ONLY valid JSON with no additional text or markdown."
    )
    return {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 1024,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64",
                                             "media_type": media_type, "data": image_b64}},
                {"type": "text", "text": prompt},
            ],
        }],
    }


def parse_analysis_response(response_body: dict) -> dict:
    for block in response_body.get("content", []):
        if block.get("type") == "text":
            text = block["text"].strip()
            if text.startswith("```"):
                lines = text.splitlines()
                text = "\n".join(lines[1:-1]) if len(lines) > 2 else text
            return json.loads(text)
    raise ValueError("No text content in Bedrock response")


def emit_all_images_analyzed(job_id: str, image_count: int) -> None:
    _events().put_events(Entries=[{
        "Source": "realestate.video.pipeline",
        "DetailType": "all-images-analyzed",
        "Detail": json.dumps({"job_id": job_id, "image_count": image_count}),
        "EventBusName": EVENT_BUS_NAME,
    }])
    logger.info(job_id=job_id, stage="image_analysis",
                outcome="all_images_analyzed_emitted", image_count=image_count)


def check_image_format(image_bytes: bytes) -> str:
    if image_bytes[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if image_bytes[:4] == b"\x89PNG":
        return "image/png"
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    raise ValueError(
        "Unsupported image format. Only JPEG, PNG, and WEBP are accepted.")
