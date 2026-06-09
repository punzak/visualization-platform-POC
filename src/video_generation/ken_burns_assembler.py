"""
Ken Burns Video Assembler — replaces Nova Reel for the video generation stage.

Takes property photos + Polly voiceover MP3 and produces a final MP4 using FFmpeg:
- Each photo gets a Ken Burns zoom/pan effect (duration = segment duration_seconds)
- Photos are concatenated in story sequence order
- Polly narration is overlaid as audio
- Output is a single MP4 uploaded to S3

Uses the ffmpeg-python library and a Lambda layer with FFmpeg binary.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from typing import Any

import boto3

from shared.dynamo import get_story_by_job, safe_update_job_status, update_job_fields
from shared.logger import StructuredLogger

REGION = os.environ.get("AWS_REGION", "us-east-1")
INPUT_BUCKET = os.environ.get("INPUT_BUCKET", "realestate-video-input")
ASSETS_BUCKET = os.environ.get("ASSETS_BUCKET", "realestate-video-assets")
OUTPUT_BUCKET = os.environ.get("OUTPUT_BUCKET", "realestate-video-output")
JOBS_TABLE = os.environ.get("JOBS_TABLE", "property-video-jobs")
STORIES_TABLE = os.environ.get("STORIES_TABLE", "property-video-stories")
EVENT_BUS_NAME = os.environ.get("EVENT_BUS_NAME", "realestate-video-pipeline")

# FFmpeg binary — provided by Lambda layer or bundled
FFMPEG_PATH = os.environ.get("FFMPEG_PATH", "/opt/bin/ffmpeg")
if not os.path.exists(FFMPEG_PATH):
    FFMPEG_PATH = "/usr/bin/ffmpeg"  # fallback

logger = StructuredLogger("video_generation.ken_burns")

s3 = boto3.client("s3", region_name=REGION)
events = boto3.client("events", region_name=REGION)
dynamodb = boto3.resource("dynamodb", region_name=REGION)

# Ken Burns effects — smooth, slow, cinematic
# Using high-res source + very slow zoom rate to eliminate jitter
ZOOM_EFFECTS = [
    "zoom_in_center",
    "zoom_out_center",
    "pan_right",
    "pan_left",
    "zoom_in_corner",
]

FPS = 24
OUTPUT_W = 1080
OUTPUT_H = 1920


def handler(event: dict, context: Any) -> dict:
    """Triggered by EventBridge voiceover-complete event."""
    job_id = event["detail"]["job_id"]
    voiceover_s3_key = event["detail"].get("voiceover_s3_key", "")

    logger.info(job_id=job_id, stage="video_generation.ken_burns",
                outcome="started")

    story = get_story_by_job(job_id)
    if story is None:
        logger.error(job_id=job_id, stage="video_generation.ken_burns",
                     outcome="story_not_found")
        return {"statusCode": 500}

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            final_video_key = assemble_video(
                job_id=job_id,
                story=story,
                voiceover_s3_key=voiceover_s3_key,
                tmpdir=tmpdir,
            )
        except Exception as e:
            logger.error(job_id=job_id, stage="video_generation.ken_burns",
                         outcome="assembly_failed", error=str(e))
            dynamodb.Table(JOBS_TABLE).update_item(
                Key={"job_id": job_id},
                UpdateExpression="SET #s = :s, error_message = :e",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":s": "failed", ":e": str(e)},
            )
            return {"statusCode": 500}

    # Update job as complete
    dynamodb.Table(JOBS_TABLE).update_item(
        Key={"job_id": job_id},
        UpdateExpression="SET #s = :s, final_video_s3_key = :v",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "complete", ":v": final_video_key},
    )

    logger.info(job_id=job_id, stage="video_generation.ken_burns",
                outcome="completed", final_video_s3_key=final_video_key)
    return {"statusCode": 200}


def build_ken_burns_filter(effect: str, frames: int, w: int, h: int) -> str:
    """
    Smooth Ken Burns using high-res zoompan + downscale.

    The key to eliminating jitter: run zoompan at 4x output resolution
    so sub-pixel movements get anti-aliased when downscaled.
    The zoompan filter works on integer pixels, so at 5120x2880 a 1-pixel
    shift becomes a 0.25-pixel shift at 1280x720 — invisible to the eye.
    """
    # Work at 4x resolution for smooth sub-pixel interpolation
    zw = w * 4   # 5120
    zh = h * 4   # 2880

    # Very slow zoom rate at high res = buttery smooth at output res
    if effect == "zoom_in_center":
        zp = (
            f"zoompan=z='min(zoom+0.001,1.3)'"
            f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":d={frames}:s={zw}x{zh}:fps={FPS}"
        )
    elif effect == "zoom_out_center":
        zp = (
            f"zoompan=z='if(lte(zoom,1.0),1.3,max(1.001,zoom-0.001))'"
            f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":d={frames}:s={zw}x{zh}:fps={FPS}"
        )
    elif effect == "pan_right":
        zp = (
            f"zoompan=z='1.25'"
            f":x='min(iw/zoom,x+2)':y='ih/2-(ih/zoom/2)'"
            f":d={frames}:s={zw}x{zh}:fps={FPS}"
        )
    elif effect == "pan_left":
        zp = (
            f"zoompan=z='1.25'"
            f":x='max(0,x-2)':y='ih/2-(ih/zoom/2)'"
            f":d={frames}:s={zw}x{zh}:fps={FPS}"
        )
    else:  # zoom_in_corner
        zp = (
            f"zoompan=z='min(zoom+0.001,1.3)'"
            f":x='iw/zoom*0.65':y='ih/zoom*0.65'"
            f":d={frames}:s={zw}x{zh}:fps={FPS}"
        )

    # Downscale from 4x to output with high-quality lanczos
    return f"scale=8000:-1,{zp},scale={w}:{h}:flags=lanczos,setsar=1"


def assemble_video(job_id: str, story, voiceover_s3_key: str, tmpdir: str) -> str:
    """Download photos + audio, run FFmpeg, upload final MP4."""
    from PIL import Image
    import io

    segments = story.segments
    if isinstance(segments, str):
        segments = json.loads(segments)
    # Sort by segment_index
    segments = sorted(
        [s.to_dict() if hasattr(s, 'to_dict') else s for s in segments],
        key=lambda x: int(x.get("segment_index", 0))
    )

    logger.info(job_id=job_id, stage="video_generation.ken_burns",
                outcome="downloading_assets", segment_count=len(segments))

    # Download and resize each photo
    photo_paths = []
    durations = []
    for i, seg in enumerate(segments):
        s3_key = seg.get("s3_key", "")
        duration = int(seg.get("duration_seconds", 5))
        durations.append(duration)

        img_bytes = s3.get_object(Bucket=INPUT_BUCKET, Key=s3_key)[
            "Body"].read()
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        # Scale to COVER the output frame (no stretching), then center-crop
        src_w, src_h = img.size
        scale = max(OUTPUT_W / src_w, OUTPUT_H / src_h)
        scaled_w = int(src_w * scale)
        scaled_h = int(src_h * scale)
        img = img.resize((scaled_w, scaled_h), Image.LANCZOS)
        left = (scaled_w - OUTPUT_W) // 2
        top = (scaled_h - OUTPUT_H) // 2
        img = img.crop((left, top, left + OUTPUT_W, top + OUTPUT_H))
        photo_path = os.path.join(tmpdir, f"photo_{i:03d}.jpg")
        img.save(photo_path, "JPEG", quality=92)
        photo_paths.append(photo_path)

    # Download voiceover
    audio_path = os.path.join(tmpdir, "narration.mp3")
    if voiceover_s3_key:
        audio_bytes = s3.get_object(Bucket=ASSETS_BUCKET, Key=voiceover_s3_key)[
            "Body"].read()
        with open(audio_path, "wb") as f:
            f.write(audio_bytes)
    else:
        audio_path = None

    # Build FFmpeg command
    # Each photo → Ken Burns clip → concat → add audio
    # Get story_id for progress updates
    story_id = story.story_id if hasattr(story, 'story_id') else None
    stories_table = dynamodb.Table(STORIES_TABLE) if story_id else None

    segment_paths = []
    for i, (photo_path, duration) in enumerate(zip(photo_paths, durations)):
        effect_name = ZOOM_EFFECTS[i % len(ZOOM_EFFECTS)]
        frames = duration * FPS
        seg_path = os.path.join(tmpdir, f"seg_{i:03d}.mp4")

        vf = build_ken_burns_filter(effect_name, frames, OUTPUT_W, OUTPUT_H)

        cmd = [
            FFMPEG_PATH, "-y",
            "-loop", "1",
            "-i", photo_path,
            "-vf", vf,
            "-t", str(duration),
            "-c:v", "libx264",
            "-preset", "fast",
            "-pix_fmt", "yuv420p",
            "-r", str(FPS),
            seg_path,
        ]
        run_ffmpeg(cmd, f"segment {i}")
        segment_paths.append(seg_path)

        # Update segment status in DynamoDB so frontend shows progress
        if stories_table and story_id:
            try:
                segments[i]["kling_status"] = "complete"
                stories_table.update_item(
                    Key={"story_id": story_id},
                    UpdateExpression="SET #segs = :segs",
                    ExpressionAttributeNames={"#segs": "segments"},
                    ExpressionAttributeValues={":segs": json.dumps(segments)},
                )
            except Exception:
                pass  # non-critical, don't fail the pipeline

    # Write concat list
    concat_list = os.path.join(tmpdir, "concat.txt")
    with open(concat_list, "w") as f:
        for p in segment_paths:
            f.write(f"file '{p}'\n")

    # Concatenate all segments
    concat_path = os.path.join(tmpdir, "concat.mp4")
    cmd = [
        FFMPEG_PATH, "-y",
        "-f", "concat", "-safe", "0",
        "-i", concat_list,
        "-c", "copy",
        concat_path,
    ]
    run_ffmpeg(cmd, "concat")

    # Add audio
    final_path = os.path.join(tmpdir, "final.mp4")
    if audio_path and os.path.exists(audio_path):
        cmd = [
            FFMPEG_PATH, "-y",
            "-i", concat_path,
            "-i", audio_path,
            "-c:v", "copy",
            "-c:a", "aac",
            "-shortest",
            final_path,
        ]
        run_ffmpeg(cmd, "add audio")
    else:
        final_path = concat_path

    # Upload to output bucket
    output_key = f"videos/{job_id}/property_tour.mp4"
    with open(final_path, "rb") as f:
        s3.put_object(
            Bucket=OUTPUT_BUCKET,
            Key=output_key,
            Body=f.read(),
            ContentType="video/mp4",
        )

    logger.info(job_id=job_id, stage="video_generation.ken_burns",
                outcome="uploaded", s3_key=output_key)
    return output_key


def run_ffmpeg(cmd: list, label: str) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg {label} failed:\n{result.stderr[-500:]}")
