"""Base interface for video generation providers."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class VideoTaskResult:
    """Result of submitting a video generation task."""
    task_id: str
    status: str          # "queued" | "processing" | "complete" | "failed"
    # True = webhook callback needed; False = video_bytes available immediately
    is_async: bool
    video_bytes: Optional[bytes] = None   # set when is_async=False
    # set when is_async=False and already uploaded
    video_s3_key: Optional[str] = None


class VideoProvider(ABC):
    """Abstract base class for all video generation providers.

    To add a new provider:
    1. Create a new file in this directory (e.g. my_provider.py)
    2. Subclass VideoProvider and implement submit_task()
    3. Register it in get_provider() in factory.py
    4. Set VIDEO_PROVIDER=my_provider in the Lambda environment
    """

    @abstractmethod
    def submit_task(
        self,
        image_url: str,
        prompt: str,
        duration_seconds: int,
        camera_movement: str,
        job_id: str,
        segment_index: int,
    ) -> VideoTaskResult:
        """Submit an image-to-video generation task.

        Args:
            image_url: URL or S3 key of the source image.
            prompt: Cinematic generation prompt.
            duration_seconds: Target clip duration.
            camera_movement: Camera movement style.
            job_id: Pipeline job ID (for logging/tracing).
            segment_index: Segment position (for logging/tracing).

        Returns:
            VideoTaskResult with task_id and async/sync indicator.
        """
