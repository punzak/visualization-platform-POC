"""Kling.ai image-to-video provider (async, webhook-based)."""
from __future__ import annotations

import os

import requests

from shared.logger import StructuredLogger
from shared.xray import begin_subsegment, end_subsegment, put_annotation
from video_generation.providers.base import VideoProvider, VideoTaskResult

logger = StructuredLogger("video_generation.kling")

KLING_API_URL = os.environ.get("KLING_API_URL", "https://api.kling.ai/v1")
WEBHOOK_URL = os.environ.get(
    "WEBHOOK_URL", "https://example.com/webhook/kling")


class KlingProvider(VideoProvider):
    """Submits tasks to Kling.ai API v3.0. Async — results delivered via webhook."""

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    def submit_task(
        self,
        image_url: str,
        prompt: str,
        duration_seconds: int,
        camera_movement: str,
        job_id: str,
        segment_index: int,
    ) -> VideoTaskResult:
        payload = {
            "image_url": image_url,
            "prompt": prompt,
            "duration": duration_seconds,
            "aspect_ratio": "16:9",
            "resolution": "1080p",
            "mode": "cinematic",
            "camera_movement": camera_movement,
            "webhook_url": WEBHOOK_URL,
        }

        begin_subsegment("kling-submit-video-task")
        put_annotation("job_id", job_id)
        put_annotation("segment_index", segment_index)
        try:
            response = requests.post(
                url=f"{KLING_API_URL}/video/generate",
                headers={"Authorization": f"Bearer {self._api_key}",
                         "Content-Type": "application/json"},
                json=payload,
                timeout=30,
            )
            if response.status_code != 202:
                raise RuntimeError(
                    f"Kling.ai error {response.status_code}: {response.text}")
            task_id = response.json().get("task_id")
            assert task_id, "Kling.ai response missing task_id"
        finally:
            end_subsegment()

        logger.info(job_id=job_id, stage="video_generation.kling",
                    outcome="task_submitted", task_id=task_id)
        return VideoTaskResult(task_id=task_id, status="queued", is_async=True)
