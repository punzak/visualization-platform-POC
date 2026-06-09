# Kling AI Real Estate Video POC

Serverless pipeline that transforms property photos into a finished MP4 showcase video using AWS Lambda, Bedrock (Claude 3.5 Sonnet), ElevenLabs TTS, and Kling.ai image-to-video.

## Architecture Overview

```
S3 Upload → ImageAnalysisFunction (Bedrock)
          → StorySequencingFunction (Bedrock)
          → VoiceoverGenerationFunction (ElevenLabs)
          → VideoGenerationOrchestratorFunction (Kling.ai)
          → KlingWebhookHandlerFunction (API Gateway)
          → Step Functions Assembly Workflow
          → Final MP4 in S3
```

Five Lambda functions are decoupled via EventBridge events. DynamoDB tracks job state throughout. All secrets are stored in Secrets Manager. Infrastructure is defined in two CloudFormation stacks.

## Prerequisites

- AWS CLI v2 configured with appropriate credentials
- Python 3.12
- boto3 (`pip install boto3`)
- An AWS account with Bedrock access to `anthropic.claude-3-5-sonnet-20241022-v2:0` in `us-east-1`
- ElevenLabs API key and voice ID
- Kling.ai API key and webhook secret

## Deployment Steps

### Step 1: Set real API keys in Secrets Manager

After deploying the IAM stack (Step 2), update the placeholder secrets with real values:

```bash
# ElevenLabs
aws secretsmanager put-secret-value \
  --secret-id elevenlabs/api_key \
  --secret-string '{"api_key":"<YOUR_ELEVENLABS_KEY>","voice_id":"<YOUR_VOICE_ID>"}'

# Kling.ai
aws secretsmanager put-secret-value \
  --secret-id kling/api_key \
  --secret-string '{"api_key":"<YOUR_KLING_KEY>"}'

aws secretsmanager put-secret-value \
  --secret-id kling/webhook_secret \
  --secret-string '{"webhook_secret":"<YOUR_WEBHOOK_SECRET>"}'
```

### Step 2: Deploy IAM and Secrets Manager stack

```bash
aws cloudformation deploy \
  --template-file infra/iam_and_secrets.yaml \
  --stack-name kling-poc-iam-and-secrets \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides AwsAccountId=<YOUR_ACCOUNT_ID>
```

### Step 3: Package Lambda code

Zip each handler directory along with the shared module:

```bash
for fn in image_analysis story_sequencing voiceover_generation video_generation webhook_handler; do
  cd src
  zip -r "../dist/${fn}.zip" "${fn}/" shared/
  cd ..
done
```

### Step 4: Upload Lambda packages to S3

```bash
BUCKET=<YOUR_BUCKET>
for fn in image_analysis story_sequencing voiceover_generation video_generation webhook_handler; do
  aws s3 cp "dist/${fn}.zip" "s3://${BUCKET}/lambda/${fn}.zip"
done
```

### Step 5: Deploy pipeline stack

```bash
aws cloudformation deploy \
  --template-file infra/pipeline_resources.yaml \
  --stack-name kling-poc-pipeline \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    IamStackName=kling-poc-iam-and-secrets \
    LambdaCodeBucket=<YOUR_BUCKET> \
    LambdaCodeKey=lambda/latest
```

## Switching Video Providers

Set the `VIDEO_PROVIDER` environment variable on the `VideoGenerationOrchestratorFunction` Lambda:

| Value | Provider | Requires | Notes |
|-------|----------|----------|-------|
| `kling` | Kling.ai API v3.0 | `kling/api_key` secret | Default. Async — results via webhook |
| `nova_reel` | Amazon Nova Reel (Bedrock) | IAM role only | No webhook needed. Synchronous polling |
| `runway` | Runway Gen-3 Alpha Turbo | `runway/api_key` secret | No webhook needed. Synchronous polling |

**To switch providers:**
```bash
aws lambda update-function-configuration \
  --function-name kling-poc-video-generation-orchestrator \
  --environment Variables="{VIDEO_PROVIDER=nova_reel,...}"
```

For `nova_reel`, no additional secrets are needed — it uses the Lambda's IAM role to call Bedrock.

For `runway`, add the API key to Secrets Manager first:
```bash
aws secretsmanager create-secret \
  --name runway/api_key \
  --secret-string '{"api_key":"<YOUR_RUNWAY_KEY>"}'
```



```bash
pip install -r requirements.txt
pytest tests/ -v
```

## Starting a Job

Create a `JobRecord` in DynamoDB and upload property images to S3 to trigger the pipeline:

```python
import boto3
import uuid

job_id = str(uuid.uuid4())
image_count = 5  # number of images you will upload

# Create the job record
dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
jobs_table = dynamodb.Table("property-video-jobs")
jobs_table.put_item(Item={
    "job_id": job_id,
    "status": "analyzing",
    "image_count": image_count,
    "images_analyzed": 0,
    "created_at": "2024-01-01T00:00:00Z",
})

# Upload images — each upload triggers ImageAnalysisFunction via S3 event
s3 = boto3.client("s3")
for i, local_path in enumerate(["photo1.jpg", "photo2.jpg", "photo3.jpg", "photo4.jpg", "photo5.jpg"]):
    s3_key = f"property_photos/{job_id}/image_{i}.jpg"
    s3.upload_file(local_path, "realestate-video-input", s3_key)
    print(f"Uploaded {s3_key}")

print(f"Job {job_id} started. Monitor status in DynamoDB property-video-jobs table.")
```

The pipeline runs automatically once all images are uploaded. Poll the job status:

```python
response = jobs_table.get_item(Key={"job_id": job_id})
print(response["Item"]["status"])  # analyzing → sequencing → voiceover → generating → assembling → complete
```

## Cost Estimate

Approximate cost per property video (5 images, ~75 second video):

| Service | Usage | Estimated Cost |
|---------|-------|----------------|
| Amazon Bedrock (Claude 3.5 Sonnet) | ~10K input + 4K output tokens | ~$0.10 |
| ElevenLabs TTS | ~500 words | ~$0.05 |
| Kling.ai image-to-video | 5 segments × ~$0.40 | ~$2.00 |
| AWS Lambda | ~30s total compute | ~$0.01 |
| S3 storage + transfer | ~500 MB | ~$0.01 |
| **Total** | | **~$2–5 per video** |

Costs vary based on script length, number of images, and Kling.ai pricing tier.
