# Nuance

**Multimodal emotion and sentiment analysis for video, with a Next.js dashboard and AWS SageMaker inference.**

Nuance combines video frames, audio features, and transcribed speech to predict emotion and sentiment for spoken segments. Users can register, upload videos, inspect a summary and segment scores, and invoke analysis through an API key.

The repository contains model training, serving code, the web application, a PostgreSQL schema, and optional AWS monitoring and data infrastructure. The UI currently uses the product name **Aspect Sentiment**. The model scores utterances and the dashboard summarizes those scores at video level. Topic extraction and target-specific aspect sentiment remain future work.

## Contents

- [Implementation status](#implementation-status)
- [Architecture](#architecture)
- [Repository layout](#repository-layout)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [API reference](#api-reference)
- [Model and training](#model-and-training)
- [SageMaker deployment](#sagemaker-deployment)
- [Database and tenancy](#database-and-tenancy)
- [Monitoring and drift](#monitoring-and-drift)
- [Optional async infrastructure](#optional-async-infrastructure)
- [Hosting and costs](#hosting-and-costs)
- [Verification](#verification)
- [Production limitations](#production-limitations)
- [Troubleshooting](#troubleshooting)
- [Documentation and attribution](#documentation-and-attribution)

## Implementation status

This README describes the checked-in Nuance code. Other guides contain historical deployment notes.

| Area | Current implementation |
| --- | --- |
| Web app | Next.js App Router, React, Tailwind, signup, credentials sign-in, dashboard, uploads, summary, and segment results |
| Database | Prisma with PostgreSQL and the Neon serverless adapter, including PostgreSQL migrations |
| Authentication | Auth.js credentials, bcrypt password checks, JWT sessions, and separate Edge-compatible configuration |
| Inference | Synchronous SageMaker invocation with an S3 pointer and upload ownership checks |
| Classification | Seven emotions and three sentiment classes, ranked by confidence |
| Usage | Per-user API keys and a rolling 30-day quota in PostgreSQL |
| Feedback | Signed prediction tokens, corrected labels, DynamoDB storage and accuracy counters |
| Operations | EMF, baseline generation, drift math, scheduled jobs, alarms, dashboards, Firehose, and Athena definitions |
| Advanced data path | Optional CDK for Aurora, RDS Proxy, DynamoDB, Valkey, encrypted S3, and async jobs |
| Silent videos | The current handler depends on Whisper speech segments. Visual-only analysis needs the continuation changes integrated and deployed. |
| Live availability | A previous endpoint was deleted to avoid idle GPU charges. Inspect the current project before attempting live inference. |

The continuation chat *Improve silent video analysis* explored silent-video support. Its working changes included sampling throughout a clip, extraction without an audio stream, and a visual-only response mode. Those changes are absent from this checkout. The two requested silent clips were not tested on the hosted app because the Vercel preview required sign-in. Treat visual-only support as pending.

## Architecture

### Current application

~~~mermaid
flowchart LR
    Browser["Browser dashboard"] --> App["Next.js API and server"]
    App --> Auth["Auth.js credentials and JWT"]
    App --> DB[("Neon PostgreSQL through Prisma")]
    App -->|"Presigned URL"| Browser
    Browser -->|"Direct video PUT"| S3[("S3 uploads")]
    App -->|"S3 object URI"| Endpoint["SageMaker PyTorch endpoint"]
    S3 --> Endpoint
    Endpoint --> Extract["FFmpeg and Whisper speech segmentation"]
    Extract --> Fusion["Text, audio, and video encoders"]
    Fusion --> Results["Emotion and sentiment scores"]
    Results --> App
    App --> Browser
    Endpoint --> Logs["CloudWatch logs and EMF"]
~~~

The API signs an upload and records its owner. The browser sends the bytes directly to S3. The inference route checks the API key, ownership, and quota, then invokes SageMaker. The container transcribes speech and extracts features for each segment.

The dashboard averages a label's returned confidence values across segments where it appears, then selects the highest-scoring emotion and sentiment. The summary is computed from segment results. It is not a separately trained video-level classifier.

### Optional production path

~~~mermaid
flowchart LR
    Web["Next.js async proxy"] --> Gateway["IAM-authenticated API Gateway"]
    Gateway --> API["Jobs Lambda"]
    API --> Keys[("DynamoDB keys, quotas, and jobs")]
    API --> Cache["Valkey rate limiter"]
    Keys --> Stream["DynamoDB Stream dispatcher"]
    Stream --> Queue["SQS and DLQ"]
    Queue --> Worker["Submission Lambda"]
    Worker --> Async["SageMaker Async"]
    Async --> Output[("S3 results")]
    Async --> SNS["SageMaker SNS"]
    SNS --> Bus["EventBridge"]
    Bus --> Completion["Completion Lambda"]
    Completion --> Keys
    Completion --> Notify["SNS completion topic"]
~~~

The optional data extension also defines Aurora Serverless v2 with RDS Proxy and a tenant/date Parquet lake with Glue and Athena. These are IaC definitions. The current dashboard still uses the synchronous path. Identity, frontend, database, and analytics integration need staging verification.

## Repository layout

~~~text
Nuance/
├── ai-video-sentiment-saas-main/
│   ├── src/app/                 Pages and API route handlers
│   ├── src/actions/             Registration
│   ├── src/server/              Prisma client and Auth.js
│   ├── src/lib/                 Quotas, feedback signing, Neon helpers
│   ├── prisma/                  PostgreSQL schema and migrations
│   └── .env.example             Environment template
├── ai-video-sentiment-model-main/
│   ├── training/               MELD loader, encoders, trainer
│   ├── deployment/             Handler, serving model, baseline builder
│   ├── monitoring/             CDK, Lambda handlers, SQL, drift tests
│   ├── train_sagemaker.py       Training launcher and artifact promotion
│   └── promote_on_complete.py  Artifact promotion recovery helper
├── neon.ts                     Neon service configuration declarations
├── ARCH.md                     Architecture notes
└── HOSTING.md                  Hosting and endpoint restoration guide
~~~

The root Node package supports Neon configuration tooling. Run the web application from the SaaS directory.

## Quick start

### Prerequisites

- Node.js 22 and npm for the frontend.
- A Neon PostgreSQL database with pooled and direct connection URLs.
- Python 3.11 for model tooling and training.
- FFmpeg for local media processing.
- An authorized AWS session and running SageMaker endpoint for live inference.

The current Prisma client uses Neon's WebSocket adapter. A generic local PostgreSQL URL alone does not establish a Neon-compatible connection.

### Install and configure

~~~powershell
git clone https://github.com/Aamodx6/Nuance.git
cd Nuance/ai-video-sentiment-saas-main
Copy-Item .env.example .env
~~~

Edit the environment before installing. The template still contains an older SQLite URL. Replace it with PostgreSQL values. The connection strings below are placeholders.

~~~dotenv
DATABASE_URL="postgresql://USER:PASSWORD@YOUR-POOLED-HOST/DATABASE?sslmode=require"
DATABASE_URL_UNPOOLED="postgresql://USER:PASSWORD@YOUR-DIRECT-HOST/DATABASE?sslmode=require"
AUTH_SECRET="GENERATE_A_RANDOM_SECRET"
AUTH_URL="http://localhost:3000"
AWS_REGION="ap-southeast-2"
AWS_INFERENCE_BUCKET="YOUR_UPLOAD_BUCKET"
AWS_INFERENCE_INPUT_PREFIX="inference/uploads/"
AWS_ENDPOINT_NAME="YOUR_RUNNING_ENDPOINT"
~~~

Generate an authentication secret locally.

~~~powershell
node -e "process.stdout.write(require('crypto').randomBytes(32).toString('hex'))"
~~~

Install dependencies and apply migrations to a new development database.

~~~powershell
npm ci
npm run db:migrate
npm run dev
~~~

Open [localhost:3000](http://localhost:3000), create an account, and sign in. An existing database needs its migration history checked before applying changes. A live upload needs server-side AWS credentials, the configured bucket, and a running endpoint.

For a production build, set the canonical HTTPS origin in AUTH_URL.

~~~powershell
npm run build
npm start
~~~

## Configuration

### Application environment

| Variable | Purpose |
| --- | --- |
| DATABASE_URL | Pooled PostgreSQL URL for the Neon Prisma adapter |
| DATABASE_URL_UNPOOLED | Direct connection for Prisma migrations |
| AUTH_SECRET | Authentication secret |
| AUTH_URL | Canonical app origin |
| AWS_REGION | Sydney, ap-southeast-2, for this project |
| AWS_INFERENCE_BUCKET | Video upload bucket |
| AWS_INFERENCE_INPUT_PREFIX | Relative prefix ending in a slash, default inference/uploads/ |
| AWS_ENDPOINT_NAME | Synchronous SageMaker endpoint |
| MODEL_VERSION | Optional short version identifier for app telemetry |
| TELEMETRY_HMAC_SECRET | Optional secret, at least 32 characters, for pseudonymous correlation |
| FEEDBACK_SIGNING_SECRET | Independent secret, at least 32 characters, for feedback tokens |
| AWS_FEEDBACK_TABLE | Provisioned DynamoDB feedback table |
| FEEDBACK_RETENTION_DAYS | Default 30, accepted range 14–365 |
| AWS_JOBS_API_URL | Optional async API base URL, read directly by the proxy |
| AWS_PROFILE | Local SDK profile selector where supported by the installed SDK |

Use the AWS SDK credential chain locally and a dedicated workload identity when hosted. Keep connection passwords and keys in ignored local files or hosting secrets.

### Optional Neon helpers

NEON_AUTH_BASE_URL, NEON_AUTH_JWKS_URL, NEON_REALTIME_URL, NEON_REALTIME_SECRET, NEON_AI_GATEWAY_TOKEN, and NEON_AI_GATEWAY_BASE_URL configure diagnostic helpers.

GET /api/neon/status checks database access, fetches JWKS and AI model availability when configured, and reports whether realtime settings exist. The main login flow uses Auth.js. These helpers do not establish realtime result delivery or run the sentiment model through an AI gateway.

## API reference

### Upload and synchronous inference

Upload and inference routes accept an Authorization header with a Bearer API key. Registration creates that key in the user's ApiQuota row.

| Request | Body | Result |
| --- | --- | --- |
| POST /api/upload-url | JSON with fileType, such as .mp4 | Presigned URL, object key, file ID, content type |
| PUT to the returned URL | Raw video with the returned Content-Type | Upload to S3 |
| POST /api/sentiment-inference | JSON containing the returned key | Model version and segment predictions |
| POST /api/feedback | Signed prediction token and both corrected labels | HTTP 204 |

Accepted upload extensions are .mp4, .mov, and .avi. Presigned uploads expire after one hour. The actual codec must also be supported by the serving image.

~~~json
{"fileType": ".mp4"}
~~~

After the S3 upload, submit its key.

~~~json
{"key": "inference/uploads/<uuid>.mp4"}
~~~

Example response, shortened to one label per classifier:

~~~json
{
  "analysis": {
    "model_version": "example-v1",
    "utterances": [
      {
        "start_time": 0.0,
        "end_time": 2.4,
        "text": "Example transcribed speech.",
        "emotions": [{"label": "joy", "confidence": 0.72}],
        "sentiments": [{"label": "positive", "confidence": 0.81}]
      }
    ]
  }
}
~~~

The handler returns the top three emotions and all three sentiments per segment. When the response includes a model version and feedback signing is configured, the API adds a feedback_token.

~~~json
{
  "token": "<feedback_token>",
  "correct_emotion": "neutral",
  "correct_sentiment": "negative"
}
~~~

A feedback token binds the prediction to its caller, expires after seven days, and is accepted once. A DynamoDB transaction stores the correction and increments an hourly accuracy counter.

| HTTP status | Typical trigger |
| --- | --- |
| 400 | Invalid extension/key, repeated analysis, or invalid feedback |
| 401 | Missing or invalid API key |
| 403 | Another user's upload or invalid feedback token |
| 404 | Missing upload record |
| 409 | Feedback already recorded |
| 429 | Quota exhausted |
| 500 | Internal upload, inference, or feedback error |
| 503 | Throttled inference capacity or unavailable feedback/async configuration |

### Async API proxy

The optional /api/async-inference route forwards POST operations for uploads and jobs, and GET for jobs/{jobId}. It signs API Gateway calls with SigV4 and forwards the product key as x-aspect-api-key. It returns HTTP 503 when AWS_JOBS_API_URL is unset. The dashboard and Prisma key issuance need adaptation to the DynamoDB async identity contract.

## Model and training

### Dataset and encoders

The model trains on [MELD](https://affective-meld.github.io/), a dialogue dataset with utterance-level emotion and sentiment annotations.

| Modality | Encoder | Feature size |
| --- | --- | --- |
| Text | Frozen BERT base uncased with learned projection | 128 |
| Video | Frozen pretrained R3D-18 backbone with replacement projection | 128 |
| Audio | Mel spectrogram, convolutional layers, projection | 128 |
| Fusion | Concatenation, linear layer, batch normalization, ReLU, dropout | 256 |

Emotion classes are anger, disgust, fear, joy, neutral, sadness, and surprise. Sentiment classes are negative, neutral, and positive. Softmax values are model scores and require calibration before interpreting them as empirical correctness probabilities.

Training includes class weights, label smoothing, grouped learning rates, a validation-loss scheduler, and TensorBoard. The best validation checkpoint is saved as model.pth. The final test evaluation uses the in-memory model after the last epoch. Those logged results should be distinguished from a new evaluation of the saved checkpoint.

### Train and promote artifacts

Use an isolated Python 3.11 environment. Training pins PyTorch 2.5.1, torchaudio 2.5.1, torchvision 0.20.1, and Transformers 4.46.3. Serving pins Transformers 4.47.1 and Whisper 20240930. The trainer imports sklearn, so include scikit-learn in a local training environment.

~~~powershell
cd ai-video-sentiment-model-main
python -m venv .venv
.venv/Scripts/Activate.ps1
python -m pip install -r training/requirements.txt scikit-learn
~~~

Prepare the MELD train/dev/test CSVs and clip layout expected by training/meld_dataset.py and training/train.py. Clips are addressed as dia<Dialogue_ID>_utt<Utterance_ID>.mp4. The loader filters missing clips and invalid MP4 headers.

Inspect train_sagemaker.py before running it. Its bucket, role, channels, artifact destination, and Region reference the original team deployment. It submits one ml.g5.xlarge job with batch size 32 and 25 epochs and incurs AWS charges.

~~~powershell
python train_sagemaker.py
~~~

The launcher promotes the training archive to models/model.tar.gz in its configured bucket. promote_on_complete.py provides recovery when local log streaming prevents promotion. Production releases should use immutable versioned artifacts because this curated object is overwritten.

The serving script defaults to inference/artifacts/model.tar.gz. Set MODEL_S3_URI explicitly to the release selected for deployment to avoid a training-to-serving prefix mismatch.

### Silent and no-speech videos

The current handler loads audio before transcription and iterates only over Whisper segments. Missing audio can fail before classification. No speech segments can produce an empty utterances array. There is no checked-in analysis_mode field or independently trained facial emotion model.

The continuation changes require integration across preprocessing, model serving, API validation, and the dashboard, followed by deployment. The MELD-trained fusion model also needs evaluation on silent clips before claiming reliable visual-only results.

## SageMaker deployment

The serving entry points are model_fn, transform_fn, and predict_video in deployment/inference.py. Model loading accepts model.pth at the archive root or under model/. The handler initializes BERT and Whisper base and installs or locates FFmpeg.

| Variable | Deployment behavior |
| --- | --- |
| INFERENCE_BUCKET | Serving input and SDK artifact bucket |
| ENDPOINT_NAME | Endpoint to create or update |
| SAGEMAKER_MODEL_NAME | Unique model registration for the release |
| SAGEMAKER_EXECUTION_ROLE_ARN | Role with artifact, input, log, and capture access |
| MODEL_VERSION | Short release identifier |
| MODEL_S3_URI | Explicit trained archive URI |
| CAPTURE_PERCENT | Default 5, set 0 to disable request capture |
| CAPTURE_S3_URI | Required when capture is enabled |
| DRIFT_SAMPLE_RATE | Default 0.05 for sampled embedding telemetry |
| INITIAL_INSTANCE_COUNT | Default 1 |
| INSTANCE_TYPE | Default ml.g5.xlarge |
| UPDATE_ENDPOINT | Set true for an intended update |
| STARTUP_TIMEOUT_SECONDS | Default 600 |
| INFERENCE_INPUT_PREFIX | Must agree with the application's prefix |

Set the environment in the model directory, then run:

~~~powershell
python deployment/deploy_endpoint.py
~~~

The deployment uses the PyTorch 2.5.1 container with Python 3.11. The packaged deployment/build-constraints.txt constrains Setuptools below 82 for Whisper's pkg_resources build dependency. The deployment passes this file through both pip constraint environment variables. Verify a fresh archive and container startup in staging before directing app traffic to it.

All Regional resources for this project belong in Sydney, ap-southeast-2. Follow its managed AWS Region restrictions when designing backups and deployment.

## Database and tenancy

Prisma is active in registration, login, quota checks, upload ownership, and feedback authorization. The application schema contains User, Account, Session, VerificationToken, ApiQuota, VideoFile, and a template Post model.

The synchronous quota defaults to 10,000 requests per user and resets after 30 elapsed days. It deducts before inference. The read-then-write update and the separate analyzed flag are not concurrency-safe reservations. Failed inference currently has no quota refund path.

The optional Aurora SQL migration is a separate schema for users, organizations, memberships, plans, subscriptions, invoices, and audit records with row-level security. The current Prisma client does not automatically use that schema.

The async data path defines hashed API keys, calendar-month quota counters, and jobs in DynamoDB, with TTL and point-in-time recovery. It reserves quota and inserts a job in one transaction. Prisma API keys currently remain readable in the database and need migration to use the hashed-key path.

## Monitoring and drift

| Component | Responsibility |
| --- | --- |
| deployment/telemetry.py | One EMF event per request with stage timings, classes, confidence, input statistics, speech, and errors |
| deployment/build_baseline.py | MELD class counts, confidence/input histograms, fused embedding mean/covariance, versioned S3 baseline |
| monitoring/runtime/drift.py | Hourly window comparison using PSI, diagonal embedding distance, and confidence changes |
| monitoring/runtime/accuracy.py | Rolling accuracy for submitted feedback |
| monitoring/app.py | CDK roles, delivery, jobs, schedules, alarms, scaling, and dashboard |
| monitoring/queries.sql | Athena examples for the normalized logs |

Metric dimensions use endpoint, model version, and class. Request/user identifiers belong in the structured event body. Drift and feedback accuracy only describe the data represented in their sampling windows.

The baseline builder needs MODEL_VERSION, MODEL_DIR, MONITORING_BUCKET, MELD_TRAIN_CSV, and MELD_TRAIN_VIDEO_DIR.

~~~powershell
python deployment/build_baseline.py
~~~

To synthesize monitoring, install its requirements and configure ENDPOINT_NAME, MODEL_VERSION, RUNBOOK_URL, INFERENCE_BUCKET, and an existing SAAS_ROLE_ARN.

~~~powershell
python -m pip install -r monitoring/requirements.txt
cd monitoring
cdk synth
~~~

The Node.js CDK CLI must be installed for this command. ENABLE_MONITORING_JOBS=true enables hourly schedules. ENABLE_AUTOSCALING=true enables synchronous scaling. Both default to disabled. Align the model version, endpoint log group, baseline, and data permissions before enabling them.

## Optional async infrastructure

ENABLE_DATA_ARCHITECTURE=1 adds the advanced resources through the existing Python CDK stack.

| Component | Definition and integration status |
| --- | --- |
| Aurora | Serverless v2 writer and reader, RDS Proxy, KMS, secrets and rotation |
| DynamoDB | On-demand key, quota, and job tables with indexes, TTL and PITR |
| Valkey | Rate limiter and quota pre-check with DynamoDB fallback |
| S3 | Encrypted video, artifact, capture, result, and lake buckets |
| Async pipeline | Stream dispatcher, SQS, submission worker, SageMaker, SNS/EventBridge, completion handler and DLQ |
| Capacity | Zero-to-one async target and a wake-from-zero policy |
| Analytics | Glue catalog, Athena workgroups, and saved view definitions |
| Recovery | Erasure helper, retention policies, and restore procedures |

The extension also requires APP_ORIGIN, ASYNC_MODEL_ARTIFACT_KEY, and ASYNC_MODEL_IMAGE_URI. Stage model artifacts into the generated bucket before enabling CREATE_ASYNC_ENDPOINT=1. Check orderable database versions, GPU quotas, identity, and network connectivity first.

The hosted identity transition, frontend async workflow, analyst authorization, aggregate publishers, direct user notification delivery, and consent-based retraining still need integration. Cross-Region backup creation is restricted in the current single-Region AWS project.

## Hosting and costs

The app is structured for Next.js hosting, including Vercel. Set the SaaS directory as the project build root, configure PostgreSQL and Auth.js, and provide the server's AWS identity and S3 CORS origin. AWS clients currently use the default credential chain. A host-specific OIDC provider needs integration if the host does not supply that chain.

The continuation chat reached a protected Vercel preview and could not complete the two-video test. A preview URL and a successful build alone do not verify the model connection.

Provisioned GPUs cost money while idle. The earlier real-time endpoint was removed after testing, with the model archive and configuration retained for recreation. Async capacity can release idle GPUs, with a cold start for the next job. Hosting, database, and storage still incur their own usage.

Cost documents contain planning estimates. Check current regional rates and measure clip length, upload size, peak traffic, and GPU throughput before budgeting. One million inference requests per day is a design target. That capacity has not been demonstrated.

## Verification

Run drift unit tests from the model directory:

~~~powershell
$env:PYTHONPATH="monitoring/runtime"
python -m unittest discover -s monitoring/tests
~~~

Run frontend checks from the SaaS directory with its environment configured:

~~~powershell
npm run check
npm run build
~~~

The training/test_logging.py smoke test needs pretrained model downloads and full Python dependencies. Historical verification covers drift math, model logging, CDK synthesis, tenant isolation, and a live browser request against an earlier deployment. Those results do not establish current PostgreSQL checkout or hosted preview health.

For acceptance testing, register a temporary user, sign in, upload a short speech clip, inspect results, and confirm one quota deduction. Test invalid keys, upload ownership, repeated requests, inference failure, concurrent calls, long videos, missing audio, and no speech. Verify feedback, drift jobs, notification delivery, and erasure independently.

## Production limitations

1. Integrate and evaluate the silent-video continuation changes on a held-out non-MELD cohort.
2. Add calibration and OOD handling, content moderation, malware scanning, media size/duration checks, and training-consent intake. These are absent from this Nuance checkout.
3. Make quotas and job processing safe under concurrency and retries, migrate readable keys, and add abuse controls.
4. Verify PostgreSQL migrations, hosted credentials, sign-in, and S3 CORS together.
5. Exercise async, monitoring, deletion, and recovery in staging. Populate lake datasets before treating the Athena views as operational.
6. Reduce detailed exception output and restrict the Neon status diagnostic route before public release.
7. Audit dependencies, verify serving build constraints, and run realistic load tests before promising production capacity.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Prisma rejects file:./db.sqlite | Replace the stale template value with pooled/direct PostgreSQL URLs |
| Migration fails to connect | Check DATABASE_URL_UNPOOLED, database access, and TLS |
| Authentication redirects incorrectly | Check AUTH_SECRET, canonical AUTH_URL, and Edge/server auth configuration |
| Upload signing or PUT fails | Check AWS session, upload permissions, CORS, and matching content type |
| Inference returns 500 | Check endpoint availability, artifact, input prefix, container logs, and dependency install |
| Video has no returned segments | Check audio and intelligible speech; visual-only support needs integration |
| Async route returns 503 | Configure AWS_JOBS_API_URL and deploy the upstream resources |
| Feedback returns 503 | Configure the table, signing secret, and AWS permissions |
| Drift has no data | Check schedule flags, log subscription, Firehose delivery, baseline version, and time window |
| GPU cost continues between demos | Inspect provisioned endpoint instances and follow the endpoint cleanup guide |

## Documentation and attribution

| Reference | Coverage |
| --- | --- |
| [Architecture](ARCH.md) | Broader AWS and application diagrams |
| [Hosting](HOSTING.md) | Hosting guidance and historical endpoint restoration |
| [Model guide](ai-video-sentiment-model-main/README.md) | Original training walkthrough |
| [SaaS guide](ai-video-sentiment-saas-main/README.md) | Web setup and API configuration |
| [Progress](ASPECT_SENTIMENT_PROGRESS.md) | Recorded tutorial and team milestones |
| [Data architecture](ai-video-sentiment-model-main/monitoring/DATA_ARCHITECTURE.md) | Access patterns, retention, erasure, recovery, and load testing |
| [Cost model](ai-video-sentiment-model-main/monitoring/COST.md) | Monitoring and inference estimates |
| [Runbook](ai-video-sentiment-model-main/monitoring/RUNBOOK.md) | Alarm response and diagnostics |
| [Historical test status](ai-video-sentiment-model-main/monitoring/TEST_STATUS.md) | Earlier checks and gaps |

Some older guides describe the original SQLite setup or earlier endpoint state. Use this README with the current source and schema when configuring this checkout.

The starting model and SaaS follow [Andreas Trolle's tutorial](https://youtu.be/Myo5kizoSk0). Team work includes AWS deployment and compatibility fixes, monitoring and drift infrastructure, feedback, PostgreSQL integration, and ongoing silent-video improvements. Both source trees retain their original MIT license and copyright notices.

[Model license](ai-video-sentiment-model-main/LICENSE.MD) · [SaaS license](ai-video-sentiment-saas-main/LICENSE.MD)
