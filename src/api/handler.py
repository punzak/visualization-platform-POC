"""
API Lambda — handles frontend requests for the Kling Video POC.

Endpoints:
  POST /jobs          — upload images, create job, trigger pipeline
  GET  /jobs/{job_id} — poll job status and return enriched response

The Lambda receives multipart form data from the frontend, uploads images
to S3, creates a DynamoDB job record, and triggers the image analysis
pipeline by uploading images to the input bucket (which fires S3 events).
"""
from __future__ import annotations

import base64
import json
import os
import time
import uuid
from typing import Any

import boto3

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

REGION = os.environ.get("AWS_REGION", "us-east-1")
INPUT_BUCKET = os.environ.get("INPUT_BUCKET", "realestate-video-input")
OUTPUT_BUCKET = os.environ.get("OUTPUT_BUCKET", "realestate-video-output")
ASSETS_BUCKET = os.environ.get("ASSETS_BUCKET", "realestate-video-assets")
JOBS_TABLE = os.environ.get("JOBS_TABLE", "property-video-jobs")
STORIES_TABLE = os.environ.get("STORIES_TABLE", "property-video-stories")

PRESIGN_EXPIRY = 3600  # 1 hour for video playback URLs

s3 = boto3.client("s3", region_name=REGION)
dynamodb = boto3.resource("dynamodb", region_name=REGION)

CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
}


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

def handler(event: dict, context: Any) -> dict:
    method = event.get("httpMethod", "")
    path = event.get("path", "")

    if method == "OPTIONS":
        return {"statusCode": 200, "headers": CORS, "body": ""}

    try:
        if method == "POST" and path == "/jobs":
            return create_job(event)
        if method == "POST" and "/start" in path:
            job_id = path.split("/jobs/")[1].split("/")[0]
            return start_job(job_id)
        if method == "GET" and path.startswith("/jobs/"):
            job_id = path.split("/jobs/")[1].strip("/")
            return get_job(job_id)
        return _resp(404, {"error": "Not found"})
    except Exception as e:
        return _resp(500, {"error": str(e)})


# ---------------------------------------------------------------------------
# POST /jobs — create job and upload images
# ---------------------------------------------------------------------------

def create_job(event: dict) -> dict:
    """
    Two-phase upload:
    1. POST /jobs with JSON {property_name, file_count, filenames[]}
       → returns job_id + presigned S3 upload URLs for each image
    2. Browser uploads images directly to S3 (bypasses API Gateway 10MB limit)
    3. POST /jobs/{id}/start → triggers pipeline
    """
    body = event.get("body", "")
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")

    try:
        data = json.loads(body) if body else {}
    except Exception:
        return _resp(400, {"error": "Invalid JSON body"})

    property_name = data.get("property_name", "")
    filenames = data.get("filenames", [])
    file_count = len(filenames)

    if file_count < 1:
        return _resp(400, {"error": "Provide at least 1 filename"})
    if file_count > 20:
        return _resp(400, {"error": "Maximum 20 images allowed"})

    job_id = str(uuid.uuid4())
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    ttl = int(time.time()) + 86400 * 7

    # Generate presigned PUT URLs for each image
    upload_urls = []
    s3_keys = []
    for i, filename in enumerate(filenames):
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "jpg"
        s3_key = f"jobs/{job_id}/{i:03d}_{filename}"
        s3_keys.append(s3_key)
        presigned_url = s3.generate_presigned_url(
            "put_object",
            Params={"Bucket": INPUT_BUCKET, "Key": s3_key,
                    "ContentType": "application/octet-stream"},
            ExpiresIn=900,
        )
        upload_urls.append(
            {"filename": filename, "s3_key": s3_key, "upload_url": presigned_url})

    # Create job record
    table = dynamodb.Table(JOBS_TABLE)
    table.put_item(Item={
        "job_id": job_id,
        "status": "uploading",
        "property_address": property_name or f"Property {job_id[:8]}",
        "image_count": file_count,
        "images_analyzed": 0,
        "s3_keys": s3_keys,
        "created_at": now,
        "updated_at": now,
        "ttl": ttl,
    })

    return _resp(201, {
        "job_id": job_id,
        "image_count": file_count,
        "status": "uploading",
        "upload_urls": upload_urls,
    })


# ---------------------------------------------------------------------------
# GET /jobs/{job_id} — poll status
# ---------------------------------------------------------------------------

def start_job(job_id: str) -> dict:
    """Mark job as analyzing — called after all images are uploaded to S3."""
    table = dynamodb.Table(JOBS_TABLE)
    resp = table.get_item(Key={"job_id": job_id})
    item = resp.get("Item")
    if not item:
        return _resp(404, {"error": "Job not found"})
    if item.get("status") != "uploading":
        return _resp(400, {"error": f"Job is already {item.get('status')}"})

    table.update_item(
        Key={"job_id": job_id},
        UpdateExpression="SET #s = :s",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "analyzing"},
    )
    return _resp(200, {"job_id": job_id, "status": "analyzing"})


def get_job(job_id: str) -> dict:
    table = dynamodb.Table(JOBS_TABLE)
    resp = table.get_item(Key={"job_id": job_id})
    item = resp.get("Item")
    if not item:
        return _resp(404, {"error": "Job not found"})

    result = {
        "job_id": job_id,
        "status": item.get("status", "unknown"),
        "property_name": item.get("property_address", ""),
        "image_count": int(item.get("image_count", 0)),
        "images_analyzed": int(item.get("images_analyzed", 0)),
        "created_at": item.get("created_at"),
        "error": item.get("error_message"),
    }

    story_id = item.get("story_sequence_id")
    if story_id:
        story = _get_story(job_id)
        if story:
            result["full_script"] = story.get("full_script", "")
            segs = story.get("segments", [])
            if isinstance(segs, str):
                import json as _json
                segs = _json.loads(segs)
            result["segments"] = [
                {
                    "segment_index": int(s.get("segment_index", 0)),
                    "room_type": s.get("room_type", ""),
                    "script_text": s.get("script_text", ""),
                    "video_prompt": s.get("video_prompt", ""),
                    "camera_movement": s.get("camera_movement", ""),
                    "status": s.get("kling_status", "pending"),
                    "thumbnail_url": _presign(INPUT_BUCKET, s["s3_key"]) if s.get("s3_key") else None,
                }
                for s in segs
            ]

    final_key = item.get("final_video_s3_key")
    if final_key and item.get("status") == "complete":
        result["final_video_url"] = _presign(OUTPUT_BUCKET, final_key)

    voiceover_key = item.get("voiceover_s3_key")
    if voiceover_key:
        result["voiceover_url"] = _presign(ASSETS_BUCKET, voiceover_key)

    return _resp(200, result)
    table = dynamodb.Table(JOBS_TABLE)
    resp = table.get_item(Key={"job_id": job_id})
    item = resp.get("Item")
    if not item:
        return _resp(404, {"error": "Job not found"})

    result = {
        "job_id": job_id,
        "status": item.get("status", "unknown"),
        "property_name": item.get("property_address", ""),
        "image_count": int(item.get("image_count", 0)),
        "images_analyzed": int(item.get("images_analyzed", 0)),
        "created_at": item.get("created_at"),
        "error": item.get("error_message"),
    }

    # Attach story/script if available
    story_id = item.get("story_sequence_id")
    if story_id:
        story = _get_story(job_id)
        if story:
            result["full_script"] = story.get("full_script", "")
            segs = story.get("segments", [])
            if isinstance(segs, str):
                import json as _json
                segs = _json.loads(segs)
            result["segments"] = [
                {
                    "segment_index": int(s.get("segment_index", 0)),
                    "room_type": s.get("room_type", ""),
                    "script_text": s.get("script_text", ""),
                    "video_prompt": s.get("video_prompt", ""),
                    "camera_movement": s.get("camera_movement", ""),
                    "status": s.get("kling_status", "pending"),
                    "thumbnail_url": _presign(INPUT_BUCKET, s["s3_key"]) if s.get("s3_key") else None,
                }
                for s in segs
            ]

    # Attach final video URL if complete
    final_key = item.get("final_video_s3_key")
    if final_key and item.get("status") == "complete":
        result["final_video_url"] = _presign(OUTPUT_BUCKET, final_key)

    # Attach voiceover URL if available
    voiceover_key = item.get("voiceover_s3_key")
    if voiceover_key:
        result["voiceover_url"] = _presign(ASSETS_BUCKET, voiceover_key)

    return _resp(200, result)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_story(job_id: str) -> dict | None:
    from boto3.dynamodb.conditions import Key
    table = dynamodb.Table(STORIES_TABLE)
    resp = table.query(
        IndexName="job_id-index",
        KeyConditionExpression=Key("job_id").eq(job_id),
    )
    items = resp.get("Items", [])
    return items[0] if items else None


def _presign(bucket: str, key: str) -> str:
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=PRESIGN_EXPIRY,
    )


def _resp(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {**CORS, "Content-Type": "application/json"},
        "body": json.dumps(body, default=str),
    }


def parse_multipart(body: bytes, boundary: str) -> list[tuple[str, bytes, str]]:
    """Parse multipart/form-data body. Returns list of (name, data, content_type)."""
    parts = []
    delimiter = f"--{boundary}".encode()
    for part in body.split(delimiter):
        if not part or part == b"--\r\n" or part == b"--":
            continue
        # Split headers from body
        if b"\r\n\r\n" not in part:
            continue
        headers_raw, data = part.split(b"\r\n\r\n", 1)
        data = data.rstrip(b"\r\n")
        headers = {}
        for line in headers_raw.decode("utf-8", errors="ignore").strip().splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()

        disposition = headers.get("content-disposition", "")
        name = ""
        filename = ""
        for token in disposition.split(";"):
            token = token.strip()
            if token.startswith('name="'):
                name = token[6:-1]
            elif token.startswith('filename="'):
                filename = token[10:-1]

        ctype = headers.get("content-type", "")
        parts.append((filename or name, data, ctype))
    return parts
