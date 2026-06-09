"""VoiceoverGenerationFunction — Stage 3 of the Kling AI Video POC pipeline.

Triggered by EventBridge story-generated event. Retrieves the narrative script
from DynamoDB, calls ElevenLabs TTS API to generate an MP3, uploads it to S3,
updates the job record, and emits a voiceover-complete EventBridge event.
"""
from __future__ import annotations

import json
import os
from typing import Any

import boto3
import requests

from shared.dynamo import get_story_by_job, safe_update_job_status, update_job_fields, update_job_status
from shared.logger import StructuredLogger
from shared.secrets import SecretsManagerClient
from shared.xray import begin_subsegment, end_subsegment, put_annotation

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

EVENT_BUS_NAME = os.environ.get("EVENT_BUS_NAME", "realestate-video-pipeline")
ASSETS_BUCKET = os.environ.get("ASSETS_BUCKET", "realestate-video-assets")
ELEVENLABS_SECRET_ID = os.environ.get(
    "ELEVENLABS_SECRET_ID", "elevenlabs/api_key")

ELEVENLABS_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"

logger = StructuredLogger("voiceover_generation")

# Lazy singletons — replaced in tests via module-level patching
_s3_client = None
_events_client = None
_secrets_client = None


def _s3():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


def _events():
    global _events_client
    if _events_client is None:
        _events_client = boto3.client("events")
    return _events_client


def _secrets():
    global _secrets_client
    if _secrets_client is None:
        _secrets_client = SecretsManagerClient()
    return _secrets_client


# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------

class VoiceoverError(Exception):
    """Raised when ElevenLabs returns a non-200 response."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"ElevenLabs error {status_code}: {message}")
        self.status_code = status_code
        self.message = message


# ---------------------------------------------------------------------------
# Lambda entry point
# ---------------------------------------------------------------------------

def handler(event: dict, context: Any) -> dict:
    """Main Lambda handler triggered by EventBridge story-generated event."""
    job_id = event["detail"]["job_id"]

    logger.info(job_id=job_id, stage="voiceover_generation", outcome="started")

    story = get_story_by_job(job_id)
    if story is None:
        logger.error(job_id=job_id, stage="voiceover_generation",
                     outcome="story_not_found")
        update_job_status(job_id, "failed")
        return {"statusCode": 500}

    secret = _secrets().get_secret(ELEVENLABS_SECRET_ID)
    api_key = secret["api_key"]
    voice_id = secret["voice_id"]

    begin_subsegment("elevenlabs-tts")
    put_annotation("job_id", job_id)
    try:
        audio_bytes = generate_voiceover(story.full_script, voice_id, api_key)
    except VoiceoverError as exc:
        logger.error(
            job_id=job_id,
            stage="voiceover_generation",
            outcome="elevenlabs_error",
            status_code=exc.status_code,
            error=exc.message,
        )
        update_job_status(job_id, "failed")
        return {"statusCode": 500}
    finally:
        end_subsegment()

    voiceover_s3_key = upload_audio(audio_bytes, job_id)

    safe_update_job_status(job_id, "voiceover", "generating")
    update_job_fields(job_id, voiceover_s3_key=voiceover_s3_key)

    emit_voiceover_complete(job_id, voiceover_s3_key)

    logger.info(
        job_id=job_id,
        stage="voiceover_generation",
        outcome="completed",
        voiceover_s3_key=voiceover_s3_key,
    )

    return {"statusCode": 200}


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------

def generate_voiceover(script: str, voice_id: str, api_key: str) -> bytes:
    """Call ElevenLabs TTS API and return audio bytes.

    Args:
        script: The narration text to convert to speech.
        voice_id: ElevenLabs voice identifier.
        api_key: ElevenLabs API key.

    Returns:
        Raw MP3 audio bytes.

    Raises:
        VoiceoverError: if ElevenLabs returns a non-200 status code.
    """
    url = ELEVENLABS_TTS_URL.format(voice_id=voice_id)
    response = requests.post(
        url=url,
        headers={"xi-api-key": api_key, "Content-Type": "application/json"},
        json={
            "text": script,
            "model_id": "eleven_multilingual_v2",
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
        },
        timeout=90,
    )
    if response.status_code != 200:
        raise VoiceoverError(response.status_code, response.text)
    return response.content


def upload_audio(audio_bytes: bytes, job_id: str) -> str:
    """Upload MP3 audio to S3 and return the S3 key.

    Args:
        audio_bytes: Raw MP3 audio content.
        job_id: Job identifier used to construct the S3 key.

    Returns:
        S3 key where the audio was stored.
    """
    s3_key = f"voiceovers/{job_id}/narration.mp3"
    _s3().put_object(
        Bucket=ASSETS_BUCKET,
        Key=s3_key,
        Body=audio_bytes,
        ContentType="audio/mpeg",
    )
    return s3_key


def emit_voiceover_complete(job_id: str, voiceover_s3_key: str) -> None:
    """Emit voiceover-complete event to EventBridge."""
    _events().put_events(
        Entries=[
            {
                "Source": "realestate.video.pipeline",
                "DetailType": "voiceover-complete",
                "Detail": json.dumps(
                    {"job_id": job_id, "voiceover_s3_key": voiceover_s3_key}
                ),
                "EventBusName": EVENT_BUS_NAME,
            }
        ]
    )
    logger.info(
        job_id=job_id,
        stage="voiceover_generation",
        outcome="voiceover_complete_emitted",
        voiceover_s3_key=voiceover_s3_key,
    )
