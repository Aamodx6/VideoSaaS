# Aspect Sentiment · Model

Aspect Sentiment scores the sentiment and emotion expressed in spoken segments of video. This repository contains the PyTorch model trained on MELD, SageMaker training and inference scripts, and the AWS monitoring stack. The companion Next.js app handles video uploads, authentication, and API access.

See the [transcript progress report](ASPECT_SENTIMENT_PROGRESS.md) for the team’s deployment evidence and remaining work.

The [AWS data architecture](monitoring/DATA_ARCHITECTURE.md) covers the optional CDK extension, data model, rollout, and recovery procedures.

In the two-repository workspace, see the [project guide](../README.md), [system architecture](../ARCH.md), and [hosting and endpoint restoration guide](../HOSTING.md).

The starting implementation follows [Andreas Trolle’s tutorial](https://youtu.be/Myo5kizoSk0). The upstream copyright and license are retained in [LICENSE.MD](LICENSE.MD).

## Setup

Follow these steps to install and set up the project.

### Clone the Repository

```bash
git clone https://github.com/Andreaswt/ai-video-sentiment-model.git
```

### Navigate to the Project Directory

```bash
cd ai-video-sentiment-model
```

### Install Python

Download and install Python if not already installed. Use the link below for guidance on installation:
[Python Download](https://www.python.org/downloads/)

### Install Dependencies

```bash
pip install -r training/requirements.txt
```

### Download the Dataset

Visit the following link to download the MELD dataset:
[MELD Dataset](https://affective-meld.github.io)

Extract the dataset and place it in the `dataset` directory.

PS: learn more about state-of-the-art model in the following [Emotion Recognition Benchmark for the MELD dataset ](https://paperswithcode.com/sota/emotion-recognition-in-conversation-on-meld).

### Start Training Job

Follow these steps to train the model in a training job using AWS SageMaker:

1. Request a quota increase for an instance for training job usage for SageMaker - e.g. ml.g5.xlarge

2. Put the dataset in an S3 bucket

3. Create a role with Policies

- AmazonSageMakerFullAccess
- Access to S3 bucket with dataset

```
{
	"Version": "2012-10-17",
	"Statement": [
		{
			"Sid": "VisualEditor0",
			"Effect": "Allow",
			"Action": [
				"s3:PutObject",
				"s3:GetObject",
				"s3:ListBucket",
				"s3:DeleteObject"
			],
			"Resource": [
				"arn:aws:s3:::your-bucket-name",
				"arn:aws:s3:::your-bucket-name/*"
			]
		}
	]
}
```

4. Run the file locally, to start the training job.

```bash
python train_sagemaker.py
```

### Deploy Endpoint

Follow these steps to deploy the model as an endpoint using AWS SageMaker:

1. Create a deployment role in AWS with permissions

- AmazonSageMakerFullAccess
- CloudWatchLogsFullAccess

```
{
	"Version": "2012-10-17",
	"Statement": [
		{
			"Sid": "VisualEditor0",
			"Effect": "Allow",
			"Action": [
				"s3:PutObject",
				"s3:GetObject",
				"s3:ListBucket",
				"s3:DeleteObject"
			],
			"Resource": [
				"arn:aws:s3:::your-bucket-name",
				"arn:aws:s3:::your-bucket-name/*"
			]
		}
	]
}
```

2. Put your model file in an S3 bucket

3. Deploy the endpoint by runnin the file locally:

```bash
python deployment/deploy_endpoint.py
```

### Invoke Endpoint

Give the SaaS workload role permission to upload videos to its S3 prefix and invoke the configured endpoint. The policy below uses placeholders for your AWS project. Set the actual endpoint name through `AWS_ENDPOINT_NAME`. The monitoring stack can attach equivalent scoped permissions during rollout.

```
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "s3:PutObject"
            ],
            "Resource": [
                "arn:aws:s3:::<INFERENCE_BUCKET>/inference/uploads/*"
            ]
        },
        {
            "Effect": "Allow",
            "Action": [
                "sagemaker:InvokeEndpoint"
            ],
            "Resource": [
                "arn:aws:sagemaker:<AWS_REGION>:<AWS_ACCOUNT_ID>:endpoint/<ENDPOINT_NAME>"
            ]
        }
    ]
}
```

The SaaS invokes the endpoint using the [AWS SDK for JavaScript](https://www.npmjs.com/package/@aws-sdk/client-sagemaker-runtime) and the standard credential chain.

### Access TensorBoard

1. Download logs to local machine:
   `aws s3 sync s3://your-bucket-name/tensorboard ./tensorboard_logs`

2. Start tensorboard server
   `tensorboard --logdir tensorboard_logs`

3. Open your browser and visit:
   [http://localhost:6006](http://localhost:6006)

## Production observability

The Python CDK stack in [monitoring](monitoring) targets an existing real-time SageMaker endpoint. It provisions log delivery, versioned S3 storage, a partitioned Athena table and workgroup, scheduled drift and accuracy jobs, scaling, alarms and a dashboard. Use the IAM role output by this stack for the monitored endpoint and a dedicated role for the SaaS.

### Rollout

1. In AWS Settings → Projects, confirm the selected Region and review Billing. Use a CDK bootstrap environment in that Region and a deploy role with permission to create the named stack and attach the scoped app policy. The stack expects the existing endpoint log group. For a new environment, first create that exact log group through your platform IaC or bootstrap an endpoint with capture disabled.
2. From `monitoring/`, install `requirements.txt` into a Python environment and use the CDK CLI to run `cdk synth`, `cdk diff`, and `cdk deploy`. Set the stack configuration below. Start with scheduled jobs and autoscaling disabled. Publish [the runbook](monitoring/RUNBOOK.md) at the configured URL and confirm the SNS email subscription.
3. On a Linux worker with FFmpeg and `deployment/requirements.txt` installed, unpack the exact release's trained weights into `MODEL_DIR`. Set `MODEL_VERSION`, `MELD_TRAIN_CSV`, `MELD_TRAIN_VIDEO_DIR`, and `MONITORING_BUCKET` to the stack bucket output. Run `python deployment/build_baseline.py`. The script fails rather than overwriting a published version. Use a publisher role allowed to put only `baselines/<model_version>.json`. It scores the MELD training clips with the serving pipeline, using Whisper transcripts for predicted labels and the CSV for ground truth.
4. Deploy the [SaaS configuration](../ai-video-sentiment-saas/README.md#aws-serving-configuration). Set the endpoint variables below and run `python deployment/deploy_endpoint.py` with SageMaker Python SDK v2 and the AWS credential chain. Set `UPDATE_ENDPOINT=true` for the existing endpoint. Save the prior endpoint config name for rollback. Verify the endpoint reaches `InService` and run an authenticated short-video request through the SaaS. Check for the request in the normalized CloudWatch log group, the date/hour partition in S3 and the dashboard. Verify capture by temporarily raising the sample rate in staging. Native capture contains only the request's opaque S3 reference, because the model output contains the transcript.
5. Submit feedback for an utterance and verify an accuracy invocation can read its completed hourly counter. Enable jobs and autoscaling via CDK after publishing the baseline and confirming quota, then check the scheduled windows. Use [the example Athena queries](monitoring/queries.sql), [runbook](monitoring/RUNBOOK.md), and [cost estimate](monitoring/COST.md) during rollout.

| Component | Environment variables |
| --- | --- |
| CDK required | `ENDPOINT_NAME`, `MODEL_VERSION`, `INFERENCE_BUCKET`, `SAAS_ROLE_ARN`, `RUNBOOK_URL`; `AWS_REGION` defaults to `ap-southeast-2`. |
| CDK operations | `ALERT_EMAIL`, `ENABLE_MONITORING_JOBS`, `ENABLE_AUTOSCALING`, `LOG_RETENTION_DAYS` (30), `EVENT_RETENTION_DAYS` (30), `CAPTURE_RETENTION_DAYS` (7), `ACCURACY_WINDOW_HOURS` (168), `DRIFT_MIN_SAMPLES` (100). Alarm thresholds are in `monitoring/app.py`. |
| Endpoint deploy | `INFERENCE_BUCKET`, `ENDPOINT_NAME`, `MODEL_VERSION`, `SAGEMAKER_MODEL_NAME` (unique per release), `SAGEMAKER_EXECUTION_ROLE_ARN` (stack output), `CAPTURE_S3_URI` (stack output), `MODEL_S3_URI` (defaults to `<MODEL_ARTIFACT_PREFIX>/model.tar.gz`), `UPDATE_ENDPOINT`, `CAPTURE_PERCENT` (5), `DRIFT_SAMPLE_RATE` (0.05). |
| S3 separation | Set matching `INFERENCE_INPUT_PREFIX` and `AWS_INFERENCE_INPUT_PREFIX` on the endpoint and app. Set `MODEL_ARTIFACT_PREFIX` on CDK and the deployment script. For production use separate prefixes, for example `inference/uploads/` and `inference/artifacts/`, and place the weights under the artifact prefix before deploying. |
| Capacity | `MIN_CAPACITY` (1), `MAX_CAPACITY` (2), `INVOCATIONS_PER_INSTANCE` (4 per minute as an initial load-test target), `SCALE_IN_COOLDOWN` (600 seconds), `SCALE_OUT_COOLDOWN` (120 seconds). |

The handler emits one EMF JSON event for each invocation that reaches the handler, including errors. Firehose normalizes the TorchServe prefix, writes compressed JSON lines to S3 and re-emits bare EMF to CloudWatch. Class, confidence and the sampled embedding represent one randomly selected successful utterance per video. Identifiers are opaque request IDs and application HMACs in the log body only. The application uses one user per tenant until it has an organization model. Keep query access restricted. Treat the sampled embeddings and pseudonyms as sensitive derived data, and use a different model version whenever weights or feature definitions change.

Drift evaluates the last closed UTC hour after a delivery grace period. It skips comparisons with too few samples. `DriftScore` is the maximum of each PSI divided by its threshold and the normalized embedding mean shift divided by its threshold. A score of 1 reaches the threshold. Embedding distance is diagonal Mahalanobis RMS against the baseline covariance. No-speech and confidence comparisons are measured relative to the baseline. Counts and confidence are descriptive; MELD is a short-utterance reference population and its drift scores are not accuracy. Accuracy uses authenticated, deduplicated labels over the last seven days of complete hours and has feedback selection bias.

Run drift math tests from the model repository with `PYTHONPATH=monitoring/runtime python -m unittest discover -s monitoring/tests -v` (PowerShell: set `$env:PYTHONPATH='monitoring/runtime'` first). Load test the endpoint with representative clips before setting the GPU ceiling. The existing SaaS SQLite database and synchronous web request design require separate capacity work before a million-user launch.

Drain pending uploads under the legacy `inference/` prefix before moving the app to `inference/uploads/`. Copy the original weights to `inference/artifacts/model.tar.gz` and use a fresh model resource name for the rollout. At 1M requests per day, average demand alone is about 694 requests per minute. Set maximum capacity from peak requests per minute divided by the measured sustainable per-instance rate, then confirm service quota and headroom for rolling updates. The default ceiling is for a small rollout.
