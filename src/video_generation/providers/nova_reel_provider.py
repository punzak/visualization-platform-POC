"""Amazon Nova Reel image-to-video provider (via AWS Bedrock, synchronous)."""
from __future__ import annotations

import base64
import json
import os
import time
import uuid

import boto3

from shared.logger import StructuredLogger
from shared.xray import begin_subsegment, end_subsegment, put_annotation
from video_generation.providers.base import VideoProvider, VideoTaskResult

logger = StructuredLogger("video_generation.nova_reel")

BEDROCK_REGION = os.environ.get("BEDROCK_REGION", "us-east-1")
ASSETS_BUCKET = os.environ.get("ASSETS_BUCKET", "realestate-video-assets")
NOVA_REEL_MODEL_ID = "amazon.nova-reel-v1:0"

# Nova Reel uses async jobs via StartAsyncInvoke — poll until complete
POLL_INTERVAL_SECONDS = 10
MAX_POLL_ATTEMPTS = 60  # 10 minutes max


class NovaReelProvider(VideoProvider):
    """Generates video using Amazon Nova Reel via Bedrock async invocation.

    Nova Reel is synchronous from the pipeline's perspective — this provider
    polls until the video is ready and returns the bytes directly, so no
    webhook handler is needed. is_async=False in the result.
    """

    def __init__(self) -> None:
        self._bedrock = boto3.client(
            "bedrock-runtime", region_name=BEDROCK_REGION)
        self._s3 = boto3.client("s3", region_name=BEDROCK_REGION)

    def submit_task(
        self,
        image_url: str,
        prompt: str,
        duration_seconds: int,
        camera_movement: str,
        job_id: str,
        segment_index: int,
    ) -> VideoTaskResult:
        # Download and resize image to 1280x720 (Nova Reel requirement)
        import requests as _requests
        from PIL import Image as _Image
        import io as _io
        img_response = _requests.get(image_url, timeout=30)
        img_response.raise_for_status()
        img = _Image.open(_io.BytesIO(img_response.content)).convert("RGB")
        img = img.resize((1280, 720), _Image.LANCZOS)
        buf = _io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        image_b64 = base64.b64encode(buf.getvalue()).decode()

        task_id = str(uuid.uuid4())
        output_s3_key = f"nova-reel-output/{job_id}/{segment_index}/{task_id}/"

        request_body = {
            "taskType": "TEXT_VIDEO",
            "textToVideoParams": {
                "text": prompt,
                "images": [{"format": "jpeg", "source": {"bytes": image_b64}}],
            },
            "videoGenerationConfig": {
                "durationSeconds": min(duration_seconds, 6),
                "fps": 24,
                "dimension": "1280x720",
            },
        }

        begin_subsegment("nova-reel-async-invoke")
        put_annotation("job_id", job_id)
        put_annotation("segment_index", segment_index)
        try:
            response = self._bedrock.start_async_invoke(
                modelId=NOVA_REEL_MODEL_ID,
                modelInput=request_body,
                outputDataConfig={
                    "s3OutputDataConfig": {
                        "s3Uri": f"s3://{ASSETS_BUCKET}/{output_s3_key}"
                    }
                },
            )
            invocation_arn = response["invocationArn"]
        finally:
            end_subsegment()

        logger.info(job_id=job_id, stage="video_generation.nova_reel",
                    outcome="async_invoke_started", invocation_arn=invocation_arn)

        # Return immediately as async — store invocation_arn as task_id
        # The video_generation handler will store this and mark segment as queued
        return VideoTaskResult(
            task_id=invocation_arn,   # use ARN as task_id for polling
            status="queued",
            is_async=True,            # don't block — poll separately
            video_s3_key=output_s3_key,
        )

    def _poll_until_complete(self, invocation_arn: str, output_prefix: str,
                             job_id: str, segment_index: int) -> bytes:
        for attempt in range(MAX_POLL_ATTEMPTS):
            response = self._bedrock.get_async_invoke(
                invocationArn=invocation_arn)
            status = response["status"]

            if status == "Completed":
                # Download the output video from S3
                output_key = f"{output_s3_key}output.mp4"
                obj = self._s3.get_object(Bucket=ASSETS_BUCKET, Key=output_key)
                return obj["Body"].read()
            elif status == "Failed":
                raise RuntimeError(
                    f"Nova Reel invocation failed: {response.get('failureMessage')}")

            logger.info(job_id=job_id, stage="video_generation.nova_reel",
                        outcome="polling", attempt=attempt + 1, status=status)
            time.sleep(POLL_INTERVAL_SECONDS)

        raise TimeoutError(
            f"Nova Reel invocation timed out after {MAX_POLL_ATTEMPTS} attempts")
