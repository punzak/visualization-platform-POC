"""VoiceoverGenerationFunction — uses Amazon Polly (no external API key needed).

Triggered by EventBridge story-generated event. Retrieves the narrative script,
calls Polly to synthesize speech, uploads MP3 to S3, emits voiceover-complete.
"""
from __future__ import annotations

import json
import os
from typing import Any

import boto3

from shared.dynamo import get_story_by_job, safe_update_job_status, update_job_fields, update_job_status
from shared.logger import StructuredLogger

EVENT_BUS_NAME = os.environ.get("EVENT_BUS_NAME", "realestate-video-pipeline")
ASSETS_BUCKET = os.environ.get("ASSETS_BUCKET", "realestate-video-assets")
# warm, professional female voice
POLLY_VOICE_ID = os.environ.get("POLLY_VOICE_ID", "Joanna")
# neural = highest quality
POLLY_ENGINE = os.environ.get("POLLY_ENGINE", "neural")

logger = StructuredLogger("voiceover_generation")

_s3 = None
_polly = None
_events = None


def _get_s3():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3")
    return _s3


def _get_polly():
    global _polly
    if _polly is None:
        _polly = boto3.client("polly", region_name="us-east-1")
    return _polly


def _get_events():
    global _events
    if _events is None:
        _events = boto3.client("events")
    return _events


def handler(event: dict, context: Any) -> dict:
    job_id = event["detail"]["job_id"]
    logger.info(job_id=job_id, stage="voiceover_generation", outcome="started")

    story = get_story_by_job(job_id)
    if story is None:
        logger.error(job_id=job_id, stage="voiceover_generation",
                     outcome="story_not_found")
        update_job_status(job_id, "failed")
        return {"statusCode": 500}

    script = story.full_script
    if not script:
        logger.error(job_id=job_id, stage="voiceover_generation",
                     outcome="empty_script")
        update_job_status(job_id, "failed")
        return {"statusCode": 500}

    # Synthesize with Polly
    try:
        response = _get_polly().synthesize_speech(
            Text=script,
            OutputFormat="mp3",
            VoiceId=POLLY_VOICE_ID,
            Engine=POLLY_ENGINE,
        )
        audio_bytes = response["AudioStream"].read()
    except Exception as e:
        logger.error(job_id=job_id, stage="voiceover_generation",
                     outcome="polly_error", error=str(e))
        update_job_status(job_id, "failed")
        return {"statusCode": 500}

    # Upload to S3
    s3_key = f"voiceovers/{job_id}/narration.mp3"
    _get_s3().put_object(
        Bucket=ASSETS_BUCKET,
        Key=s3_key,
        Body=audio_bytes,
        ContentType="audio/mpeg",
    )

    safe_update_job_status(job_id, "voiceover", "generating")
    update_job_fields(job_id, voiceover_s3_key=s3_key)

    _get_events().put_events(Entries=[{
        "Source": "realestate.video.pipeline",
        "DetailType": "voiceover-complete",
        "Detail": json.dumps({"job_id": job_id, "voiceover_s3_key": s3_key}),
        "EventBusName": EVENT_BUS_NAME,
    }])

    logger.info(job_id=job_id, stage="voiceover_generation",
                outcome="completed", voiceover_s3_key=s3_key,
                audio_bytes=len(audio_bytes))
    return {"statusCode": 200}
