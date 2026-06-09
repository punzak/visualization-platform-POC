"""Runway ML Gen-3 image-to-video provider (async, polling-based)."""
from __future__ import annotations

import os
import time
import uuid

import requests

from shared.logger import StructuredLogger
from shared.xray import begin_subsegment, end_subsegment, put_annotation
from video_generation.providers.base import VideoProvider, VideoTaskResult

logger = StructuredLogger("video_generation.runway")

RUNWAY_API_URL = os.environ.get(
    "RUNWAY_API_URL", "https://api.dev.runwayml.com/v1")
POLL_INTERVAL_SECONDS = 10
MAX_POLL_ATTEMPTS = 60


class RunwayProvider(VideoProvider):
    """Generates video using Runway Gen-3 Alpha Turbo API.

    Runway uses a task-based async model with polling (no webhooks).
    This provider polls until complete and returns video bytes directly.
    """

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
            "model": "gen3a_turbo",
            "promptImage": image_url,
            "promptText": f"{prompt}. Camera: {camera_movement}.",
            "duration": min(duration_seconds, 10),  # Runway max 10s
            "ratio": "1280:768",
            "watermark": False,
        }

        begin_subsegment("runway-submit-task")
        put_annotation("job_id", job_id)
        put_annotation("segment_index", segment_index)
        try:
            response = requests.post(
                url=f"{RUNWAY_API_URL}/image_to_video",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                    "X-Runway-Version": "2024-11-06",
                },
                json=payload,
                timeout=30,
            )
            if response.status_code not in (200, 201):
                raise RuntimeError(
                    f"Runway error {response.status_code}: {response.text}")
            task_id = response.json().get("id")
            assert task_id, "Runway response missing task id"
        finally:
            end_subsegment()

        logger.info(job_id=job_id, stage="video_generation.runway",
                    outcome="task_submitted", task_id=task_id)

        # Poll until complete
        video_url = self._poll_until_complete(task_id, job_id)
        video_bytes = requests.get(video_url, timeout=60).content

        return VideoTaskResult(
            task_id=task_id,
            status="complete",
            is_async=False,
            video_bytes=video_bytes,
        )

    def _poll_until_complete(self, task_id: str, job_id: str) -> str:
        for attempt in range(MAX_POLL_ATTEMPTS):
            response = requests.get(
                url=f"{RUNWAY_API_URL}/tasks/{task_id}",
                headers={"Authorization": f"Bearer {self._api_key}",
                         "X-Runway-Version": "2024-11-06"},
                timeout=15,
            )
            data = response.json()
            status = data.get("status")

            if status == "SUCCEEDED":
                return data["output"][0]
            elif status == "FAILED":
                raise RuntimeError(
                    f"Runway task failed: {data.get('failure')}")

            logger.info(job_id=job_id, stage="video_generation.runway",
                        outcome="polling", attempt=attempt + 1, status=status)
            time.sleep(POLL_INTERVAL_SECONDS)

        raise TimeoutError(
            f"Runway task timed out after {MAX_POLL_ATTEMPTS} attempts")
