"""
Nova Reel Poller Lambda — checks status of all queued Nova Reel invocations
for a job and marks segments complete when their video is ready.

Triggered by EventBridge Scheduler every 60 seconds while job is in 'generating' state.
When all segments are complete, emits all-segments-complete event.
"""
from __future__ import annotations

import json
import os
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

REGION = os.environ.get("BEDROCK_REGION", "us-east-1")
ASSETS_BUCKET = os.environ.get("ASSETS_BUCKET", "realestate-video-assets")
JOBS_TABLE = os.environ.get("JOBS_TABLE", "property-video-jobs")
STORIES_TABLE = os.environ.get("STORIES_TABLE", "property-video-stories")
EVENT_BUS_NAME = os.environ.get("EVENT_BUS_NAME", "realestate-video-pipeline")

bedrock = boto3.client("bedrock-runtime", region_name=REGION)
s3 = boto3.client("s3", region_name=REGION)
dynamodb = boto3.resource("dynamodb", region_name=REGION)
events = boto3.client("events", region_name=REGION)


def handler(event: dict, context: Any) -> dict:
    """Poll all 'generating' jobs and check Nova Reel invocation status."""
    jobs_table = dynamodb.Table(JOBS_TABLE)
    stories_table = dynamodb.Table(STORIES_TABLE)

    # Scan for jobs in 'generating' status
    resp = jobs_table.scan(
        FilterExpression="attribute_exists(job_id) AND #s = :s",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "generating"},
    )

    for job in resp.get("Items", []):
        job_id = job["job_id"]
        try:
            process_job(job_id, stories_table, jobs_table)
        except Exception as e:
            print(f"Error processing job {job_id}: {e}")

    return {"statusCode": 200}


def process_job(job_id: str, stories_table, jobs_table) -> None:
    # Get story with segments
    resp = stories_table.query(
        IndexName="job_id-index",
        KeyConditionExpression=Key("job_id").eq(job_id),
    )
    items = resp.get("Items", [])
    if not items:
        return

    story = items[0]
    segs = story.get("segments", [])
    if isinstance(segs, str):
        segs = json.loads(segs)

    all_complete = True
    updated = False

    for seg in segs:
        status = seg.get("kling_status", "pending")
        if status == "complete":
            continue

        invocation_arn = seg.get("kling_task_id")
        if not invocation_arn or not invocation_arn.startswith("arn:"):
            all_complete = False
            continue

        # Check Nova Reel status
        try:
            inv_resp = bedrock.get_async_invoke(invocationArn=invocation_arn)
            inv_status = inv_resp["status"]
        except Exception as e:
            print(
                f"  Segment {seg['segment_index']}: error checking {invocation_arn}: {e}")
            all_complete = False
            continue

        if inv_status == "Completed":
            # Find the output video in S3
            output_prefix = seg.get("video_s3_key", "")
            output_key = f"{output_prefix}output.mp4"
            try:
                s3.head_object(Bucket=ASSETS_BUCKET, Key=output_key)
                # Copy to clean segment path
                final_key = f"segments/{job_id}/{seg['segment_index']}.mp4"
                s3.copy_object(
                    Bucket=ASSETS_BUCKET,
                    CopySource={"Bucket": ASSETS_BUCKET, "Key": output_key},
                    Key=final_key,
                )
                seg["kling_status"] = "complete"
                seg["video_s3_key"] = final_key
                updated = True
                print(
                    f"  Segment {seg['segment_index']}: complete → {final_key}")
            except Exception as e:
                print(
                    f"  Segment {seg['segment_index']}: completed but no output yet: {e}")
                all_complete = False
        elif inv_status == "Failed":
            seg["kling_status"] = "failed"
            updated = True
            print(
                f"  Segment {seg['segment_index']}: FAILED — {inv_resp.get('failureMessage')}")
        else:
            all_complete = False
            print(f"  Segment {seg['segment_index']}: {inv_status}")

    if updated:
        stories_table.update_item(
            Key={"story_id": story["story_id"]},
            UpdateExpression="SET #segs = :segs",
            ExpressionAttributeNames={"#segs": "segments"},
            ExpressionAttributeValues={":segs": json.dumps(segs)},
        )

    if all_complete and all(s.get("kling_status") in ("complete", "failed") for s in segs):
        print(
            f"  All segments done for {job_id} — emitting all-segments-complete")
        jobs_table.update_item(
            Key={"job_id": job_id},
            UpdateExpression="SET #s = :s",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "assembling"},
        )
        events.put_events(Entries=[{
            "Source": "realestate.video.pipeline",
            "DetailType": "all-segments-complete",
            "Detail": json.dumps({"job_id": job_id}),
            "EventBusName": EVENT_BUS_NAME,
        }])
