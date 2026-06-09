"""StorySequencingFunction — Stage 2 of the Kling AI Video POC pipeline.

Triggered by EventBridge all-images-analyzed event. Queries all image metadata
for the job, invokes Claude 3.5 Sonnet to generate a narrative story sequence
with per-scene video prompts, persists the result to DynamoDB, and emits a
story-generated EventBridge event.
"""
from __future__ import annotations

import json
import os
import uuid
from typing import Any

import boto3

from shared.dynamo import (
    put_story_sequence,
    query_images_by_job,
    safe_update_job_status,
    update_job_fields,
    update_job_status,
)
from shared.logger import StructuredLogger
from shared.models import ImageAnalysisResult, SceneSegment, StorySequence
from shared.utils import now_iso
from shared.xray import begin_subsegment, end_subsegment, put_annotation

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

EVENT_BUS_NAME = os.environ.get("EVENT_BUS_NAME", "realestate-video-pipeline")
BEDROCK_MODEL_ID = os.environ.get(
    "BEDROCK_MODEL_ID", "anthropic.claude-3-5-sonnet-20241022-v2:0"
)
BEDROCK_REGION = os.environ.get("BEDROCK_REGION", "us-east-1")

VALID_CAMERA_MOVEMENTS = [
    "slow_zoom_in", "slow_zoom_out", "pan_left", "pan_right",
    "tilt_up", "tilt_down", "dolly_forward", "dolly_backward",
    "orbit_left", "orbit_right", "static", "handheld",
]

logger = StructuredLogger("story_sequencing")

# Lazy singletons — replaced in tests via module-level patching
_bedrock_client = None
_events_client = None


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


# ---------------------------------------------------------------------------
# Lambda entry point
# ---------------------------------------------------------------------------

def handler(event: dict, context: Any) -> dict:
    """Main Lambda handler triggered by EventBridge all-images-analyzed event."""
    job_id = event["detail"]["job_id"]

    logger.info(job_id=job_id, stage="story_sequencing", outcome="started")

    story = sequence_images(job_id)

    # Update job status and story_sequence_id
    safe_update_job_status(job_id, "sequencing", "voiceover")
    update_job_fields(job_id, story_sequence_id=story.story_id)

    emit_story_generated(job_id, story.story_id)

    logger.info(
        job_id=job_id,
        stage="story_sequencing",
        outcome="completed",
        story_id=story.story_id,
    )

    return {"statusCode": 200}


# ---------------------------------------------------------------------------
# Core sequencing logic
# ---------------------------------------------------------------------------

def sequence_images(job_id: str) -> StorySequence:
    """Query image metadata, invoke Bedrock, validate and persist StorySequence."""
    images = query_images_by_job(job_id)
    if not images:
        raise ValueError(f"No images found for job_id={job_id!r}")

    prompt = build_bedrock_prompt(images)

    request_body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": prompt}],
    }

    begin_subsegment("bedrock-invoke-model")
    put_annotation("job_id", job_id)
    put_annotation("model_id", BEDROCK_MODEL_ID)
    try:
        bedrock_response = _bedrock().invoke_model(
            modelId=BEDROCK_MODEL_ID,
            body=json.dumps(request_body),
            contentType="application/json",
            accept="application/json",
        )
        response_body = json.loads(bedrock_response["body"].read())
    finally:
        end_subsegment()
    story_dict = parse_story_response(response_body)

    # Validate duration
    total_duration = int(story_dict.get("total_duration_seconds", 0))
    if not (60 <= total_duration <= 90):
        logger.error(
            job_id=job_id,
            stage="story_sequencing",
            outcome="invalid_duration",
            total_duration_seconds=total_duration,
        )
        raise ValueError(
            f"total_duration_seconds must be between 60 and 90, got {total_duration}"
        )

    # Validate segment count — allow Claude to use a subset of images (min 2)
    segments_data = story_dict.get("segments", [])
    if len(segments_data) < 2:
        logger.error(
            job_id=job_id,
            stage="story_sequencing",
            outcome="too_few_segments",
            segment_count=len(segments_data),
        )
        raise ValueError(f"Need at least 2 segments, got {len(segments_data)}")

    # Build and validate each segment
    segments = []
    for seg_data in segments_data:
        script_text = seg_data.get("script_text", "")
        video_prompt = seg_data.get("video_prompt", "")
        camera_movement = seg_data.get("camera_movement", "")

        if not script_text:
            logger.error(
                job_id=job_id,
                stage="story_sequencing",
                outcome="empty_script_text",
                segment_index=seg_data.get("segment_index"),
            )
            raise ValueError(
                f"Segment {seg_data.get('segment_index')} has empty script_text"
            )
        if not video_prompt:
            logger.error(
                job_id=job_id,
                stage="story_sequencing",
                outcome="empty_video_prompt",
                segment_index=seg_data.get("segment_index"),
            )
            raise ValueError(
                f"Segment {seg_data.get('segment_index')} has empty video_prompt"
            )
        if camera_movement not in VALID_CAMERA_MOVEMENTS:
            logger.error(
                job_id=job_id,
                stage="story_sequencing",
                outcome="invalid_camera_movement",
                camera_movement=camera_movement,
                segment_index=seg_data.get("segment_index"),
            )
            raise ValueError(
                f"Invalid camera_movement {camera_movement!r}. "
                f"Must be one of: {VALID_CAMERA_MOVEMENTS}"
            )

        segments.append(
            SceneSegment(
                segment_index=int(seg_data["segment_index"]),
                image_id=seg_data["image_id"],
                s3_key=seg_data["s3_key"],
                script_text=script_text,
                video_prompt=video_prompt,
                duration_seconds=int(seg_data.get("duration_seconds", 5)),
                camera_movement=camera_movement,
            )
        )

    story = StorySequence(
        story_id=str(uuid.uuid4()),
        job_id=job_id,
        full_script=story_dict.get("full_script", ""),
        total_duration_seconds=total_duration,
        segments=segments,
        created_at=now_iso(),
    )

    put_story_sequence(story)

    logger.info(
        job_id=job_id,
        stage="story_sequencing",
        outcome="story_persisted",
        story_id=story.story_id,
        segment_count=len(segments),
    )

    return story


# ---------------------------------------------------------------------------
# Bedrock prompt builder
# ---------------------------------------------------------------------------

def build_bedrock_prompt(images: list[ImageAnalysisResult]) -> str:
    """Construct the story sequencing prompt for Claude."""
    image_metadata_lines = []
    for i, img in enumerate(images):
        image_metadata_lines.append(
            f"Image {i + 1}:\n"
            f"  image_id: {img.image_id}\n"
            f"  s3_key: {img.s3_key}\n"
            f"  room_type: {img.room_type}\n"
            f"  architectural_style: {img.architectural_style}\n"
            f"  key_selling_points: {', '.join(img.key_selling_points)}\n"
            f"  lighting_quality: {img.lighting_quality}\n"
            f"  ambiance: {img.ambiance}\n"
            f"  composition_score: {img.composition_score}"
        )

    image_metadata_str = "\n\n".join(image_metadata_lines)
    valid_movements_str = ", ".join(VALID_CAMERA_MOVEMENTS)

    return (
        "You are a professional real estate video producer. "
        "Given the following property image metadata, create a compelling 60-90 second "
        "narrative video script that showcases the property.\n\n"
        f"Property Images:\n{image_metadata_str}\n\n"
        "Return a JSON object with exactly these fields:\n"
        "- full_script (string): complete 60-90 second narration for the entire video\n"
        "- total_duration_seconds (int): total video duration, must be between 60 and 90\n"
        "- segments (array): one segment per image, in the order you recommend showing them\n\n"
        "Each segment must have:\n"
        "  - segment_index (int): 0-based index\n"
        "  - image_id (string): the image_id from the input metadata\n"
        "  - s3_key (string): the s3_key from the input metadata\n"
        "  - script_text (string): voiceover narration for this scene\n"
        "  - video_prompt (string): cinematic prompt for Kling.ai video generation\n"
        "  - duration_seconds (int): clip duration between 3 and 10 seconds\n"
        f"  - camera_movement (string): one of [{valid_movements_str}]\n\n"
        "Return ONLY valid JSON with no additional text or markdown."
    )


# ---------------------------------------------------------------------------
# Response parser
# ---------------------------------------------------------------------------

def parse_story_response(response_body: dict) -> dict:
    """Extract and parse JSON from Claude's response, handling markdown fences."""
    content = response_body.get("content", [])
    for block in content:
        if block.get("type") == "text":
            text = block["text"].strip()
            # Strip markdown code fences if present
            if text.startswith("```"):
                lines = text.splitlines()
                text = "\n".join(lines[1:-1]) if len(lines) > 2 else text
            return json.loads(text)
    raise ValueError("No text content found in Bedrock response")


# ---------------------------------------------------------------------------
# EventBridge emission
# ---------------------------------------------------------------------------

def emit_story_generated(job_id: str, story_id: str) -> None:
    """Emit the story-generated event to EventBridge."""
    _events().put_events(
        Entries=[
            {
                "Source": "realestate.video.pipeline",
                "DetailType": "story-generated",
                "Detail": json.dumps({"job_id": job_id, "story_id": story_id}),
                "EventBusName": EVENT_BUS_NAME,
            }
        ]
    )
    logger.info(
        job_id=job_id,
        stage="story_sequencing",
        outcome="story_generated_emitted",
        story_id=story_id,
    )
