"""Lambda function that retrieves all video segment S3 keys for a job, ordered by segment_index."""
from __future__ import annotations
from shared.logger import StructuredLogger
from shared.dynamo import query_segments_by_job

import os
import sys

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..")))


logger = StructuredLogger("retrieve_segments")


def handler(event: dict, context) -> dict:
    """
    Input event: {"job_id": str}
    Returns: {"job_id": str, "segments": [{"segment_index": int, "video_s3_key": str}, ...]}
    Raises ValueError if any segment is missing video_s3_key (not yet complete)
    """
    job_id = event["job_id"]

    raw_segments = query_segments_by_job(job_id)

    # Sort by segment_index
    raw_segments.sort(key=lambda s: int(s.get("segment_index", 0)))

    segments = []
    for seg in raw_segments:
        video_s3_key = seg.get("video_s3_key") or ""
        if not video_s3_key:
            raise ValueError(
                f"Segment {seg.get('segment_index')} for job {job_id} has no video_s3_key"
            )
        segments.append({
            "segment_index": int(seg["segment_index"]),
            "video_s3_key": video_s3_key,
        })

    logger.info(job_id, "retrieve_segments",
                "success", segment_count=len(segments))

    return {"job_id": job_id, "segments": segments}
