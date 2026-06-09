"""Data models for the Kling AI Video POC pipeline."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class JobStatus(str, Enum):
    ANALYZING = "analyzing"
    SEQUENCING = "sequencing"
    VOICEOVER = "voiceover"
    GENERATING = "generating"
    ASSEMBLING = "assembling"
    COMPLETE = "complete"
    FAILED = "failed"


class KlingStatus(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETE = "complete"
    FAILED = "failed"


@dataclass
class JobRecord:
    job_id: str
    status: str
    created_at: str
    updated_at: str
    property_address: str
    image_count: int
    images_analyzed: int = 0
    story_sequence_id: Optional[str] = None
    voiceover_s3_key: Optional[str] = None
    final_video_s3_key: Optional[str] = None
    error_message: Optional[str] = None
    ttl: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "property_address": self.property_address,
            "image_count": self.image_count,
            "images_analyzed": self.images_analyzed,
            "story_sequence_id": self.story_sequence_id,
            "voiceover_s3_key": self.voiceover_s3_key,
            "final_video_s3_key": self.final_video_s3_key,
            "error_message": self.error_message,
            "ttl": self.ttl,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "JobRecord":
        return cls(
            job_id=data["job_id"],
            status=data["status"],
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            property_address=data["property_address"],
            image_count=int(data["image_count"]),
            images_analyzed=int(data.get("images_analyzed", 0)),
            story_sequence_id=data.get("story_sequence_id"),
            voiceover_s3_key=data.get("voiceover_s3_key"),
            final_video_s3_key=data.get("final_video_s3_key"),
            error_message=data.get("error_message"),
            ttl=int(data["ttl"]) if data.get("ttl") is not None else None,
        )


@dataclass
class ImageAnalysisResult:
    image_id: str
    job_id: str
    s3_key: str
    sequence_index: int
    room_type: str
    architectural_style: str
    key_selling_points: list
    lighting_quality: str
    ambiance: str
    composition_score: float
    analysis_timestamp: str

    def to_dict(self) -> dict:
        return {
            "image_id": self.image_id,
            "job_id": self.job_id,
            "s3_key": self.s3_key,
            "sequence_index": self.sequence_index,
            "room_type": self.room_type,
            "architectural_style": self.architectural_style,
            "key_selling_points": self.key_selling_points,
            "lighting_quality": self.lighting_quality,
            "ambiance": self.ambiance,
            "composition_score": self.composition_score,
            "analysis_timestamp": self.analysis_timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ImageAnalysisResult":
        return cls(
            image_id=data["image_id"],
            job_id=data["job_id"],
            s3_key=data["s3_key"],
            sequence_index=int(data["sequence_index"]),
            room_type=data["room_type"],
            architectural_style=data["architectural_style"],
            key_selling_points=data["key_selling_points"],
            lighting_quality=data["lighting_quality"],
            ambiance=data["ambiance"],
            composition_score=float(data["composition_score"]),
            analysis_timestamp=data["analysis_timestamp"],
        )


@dataclass
class SceneSegment:
    segment_index: int
    image_id: str
    s3_key: str
    script_text: str
    video_prompt: str
    duration_seconds: int
    camera_movement: str
    kling_task_id: Optional[str] = None
    kling_status: Optional[str] = None
    video_s3_key: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "segment_index": self.segment_index,
            "image_id": self.image_id,
            "s3_key": self.s3_key,
            "script_text": self.script_text,
            "video_prompt": self.video_prompt,
            "duration_seconds": self.duration_seconds,
            "camera_movement": self.camera_movement,
            "kling_task_id": self.kling_task_id,
            "kling_status": self.kling_status,
            "video_s3_key": self.video_s3_key,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SceneSegment":
        return cls(
            segment_index=int(data["segment_index"]),
            image_id=data["image_id"],
            s3_key=data["s3_key"],
            script_text=data["script_text"],
            video_prompt=data["video_prompt"],
            duration_seconds=int(data["duration_seconds"]),
            camera_movement=data["camera_movement"],
            kling_task_id=data.get("kling_task_id"),
            kling_status=data.get("kling_status"),
            video_s3_key=data.get("video_s3_key"),
        )


@dataclass
class StorySequence:
    story_id: str
    job_id: str
    full_script: str
    total_duration_seconds: int
    segments: list
    created_at: str

    def to_dict(self) -> dict:
        return {
            "story_id": self.story_id,
            "job_id": self.job_id,
            "full_script": self.full_script,
            "total_duration_seconds": self.total_duration_seconds,
            "segments": [
                s.to_dict() if isinstance(s, SceneSegment) else s
                for s in self.segments
            ],
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "StorySequence":
        segments = [
            SceneSegment.from_dict(s) if isinstance(s, dict) else s
            for s in data.get("segments", [])
        ]
        return cls(
            story_id=data["story_id"],
            job_id=data["job_id"],
            full_script=data["full_script"],
            total_duration_seconds=int(data["total_duration_seconds"]),
            segments=segments,
            created_at=data["created_at"],
        )


@dataclass
class KlingWebhookPayload:
    task_id: str
    status: str
    video_url: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    duration_seconds: Optional[float] = None
    created_at: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "video_url": self.video_url,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "duration_seconds": self.duration_seconds,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "KlingWebhookPayload":
        return cls(
            task_id=data["task_id"],
            status=data["status"],
            video_url=data.get("video_url"),
            error_code=data.get("error_code"),
            error_message=data.get("error_message"),
            duration_seconds=float(data["duration_seconds"]) if data.get(
                "duration_seconds") is not None else None,
            created_at=int(data["created_at"]) if data.get(
                "created_at") is not None else None,
        )
