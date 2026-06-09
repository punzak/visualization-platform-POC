"""Lambda function that updates the job record on success or failure."""
from __future__ import annotations
from shared.logger import StructuredLogger
from shared.dynamo import safe_update_job_status, update_job_fields, update_job_status

import os
import sys

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..")))


logger = StructuredLogger("finalize_job")


def handler(event: dict, context) -> dict:
    """
    Input event on success: {"job_id": str, "final_video_s3_key": str}
    Input event on failure: {"job_id": str, "error": str}  (called from Catch state)
    Returns: {"statusCode": 200}
    """
    job_id = event["job_id"]
    error = event.get("error")

    if error:
        # Failure path — unconditional transition to failed
        update_job_status(job_id, "failed")
        update_job_fields(job_id, error_message=str(error))
        logger.error(job_id, "finalize_job", "failed", error=str(error))
    else:
        # Success path — conditional transition assembling → complete
        final_video_s3_key = event["final_video_s3_key"]
        safe_update_job_status(job_id, "assembling", "complete")
        update_job_fields(job_id, final_video_s3_key=final_video_s3_key)
        logger.info(job_id, "finalize_job", "complete",
                    final_video_s3_key=final_video_s3_key)

    return {"statusCode": 200}
