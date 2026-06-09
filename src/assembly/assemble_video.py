"""Lambda function that concatenates video segments into a final MP4."""
from __future__ import annotations
from shared.logger import StructuredLogger
import boto3

import os
import sys

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..")))


logger = StructuredLogger("assemble_video")

ASSETS_BUCKET = os.environ.get("ASSETS_BUCKET", "realestate-video-assets")
OUTPUT_BUCKET = os.environ.get("OUTPUT_BUCKET", "realestate-video-output")

_s3 = boto3.client("s3")


def handler(event: dict, context) -> dict:
    """
    Input event: {"job_id": str, "segments": [{"segment_index": int, "video_s3_key": str}, ...]}
    Returns: {"job_id": str, "final_video_s3_key": str}
    """
    job_id = event["job_id"]
    segments = event["segments"]

    # Download each segment in order and concatenate bytes
    assembled = b""
    for seg in segments:
        s3_key = seg["video_s3_key"]
        response = _s3.get_object(Bucket=ASSETS_BUCKET, Key=s3_key)
        assembled += response["Body"].read()

    # Upload assembled video to output bucket
    output_key = f"final/{job_id}/property_tour.mp4"
    _s3.put_object(
        Bucket=OUTPUT_BUCKET,
        Key=output_key,
        Body=assembled,
        ContentType="video/mp4",
    )

    logger.info(job_id, "assemble_video", "success", output_key=output_key)

    return {"job_id": job_id, "final_video_s3_key": output_key}
