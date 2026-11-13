# Aspect Sentiment · Project progress against the tutorial

**Status as of 14 November 2026.** SageMaker training completed and the existing real-time endpoint in **console dot dream** in Sydney (`ap-southeast-2`) served a request from the local SaaS. The SQLite schema, credentials login, and authenticated dashboard were also verified locally. Hosted deployment and production monitoring are pending.

## Integration verification in tutorial order

1. Confirmed the completed SageMaker training and active serving configuration before wiring the frontend. The live endpoint was `InService` under `sentiment-analysis-endpoint-media-compat`.
2. Compared the team’s VideoSaaS reference at commit `f9f8065`. Its Prisma schema matches the companion app and its deployment configuration points to the same Sydney project, bucket, and endpoint. The frontend uses the live project values and the existing AWS session.
3. Generated and applied the initial Prisma migration to local SQLite. Prisma confirmed the schema was up to date. Auth.js issued a credentials session for a test user, and the authenticated dashboard rendered its upload controls.
4. Exercised the upload and inference APIs with a short MELD clip. Missing API key returned HTTP 401, the signed S3 upload returned HTTP 200, and the inference API returned HTTP 200 with one spoken segment, negative sentiment, and surprise emotion. The app marked the file analyzed, recorded exactly one quota use, and rejected a repeat request. The test upload and temporary database rows were removed.
5. The browser upload control now uses the exact content type signed by the server and lets the user retry after an error. The smoke test called the upload and inference APIs directly. Browser file selection and result rendering, multi-utterance clips, and the hosted app with a shared database still need verification.

[Team VideoSaaS reference](https://github.com/Aamodx6/VideoSaaS/tree/f9f8065cc576d593223f39350232ef84cea13e53)

## Transcript walkthrough

| Tutorial milestone | Team progress | Evidence and remaining work |
| --- | --- | --- |
| Prepare MELD and split the data for training, validation, and testing | Used in the completed training run | SageMaker recorded three input channels. The local [MELD dataset loader](training/meld_dataset.py) prepares video frames, audio features, and text for each labeled utterance. |
| Build the multimodal model | Implemented and trained | The [PyTorch model](training/models.py) combines video features from R3D-18, audio features from mel spectrograms, and text features from BERT. It fuses those features and predicts one of seven emotion labels and one of three sentiment labels. |
| Train on a SageMaker GPU and keep the best validation checkpoint | Verified in AWS | Training job `pytorch-training-2026-11-10-04-34-48-919` completed on `ml.g5.xlarge`. The [training script](training/train.py) saves weights when validation loss improves. SageMaker recorded the output at `s3://aspect-sentiment-lab/training-output/pytorch-training-2026-11-10-04-34-48-919/output/model.tar.gz`. |
| Review evaluation and TensorBoard | Output verified in AWS | The training log records held-out test metrics, and TensorBoard event data exists under `s3://aspect-sentiment-lab/tensorboard/`. The training launcher in the local repository still uses example bucket and execution-role values that must be supplied for a rerun. |
| Package the checkpoint and create a real-time endpoint | Verified in AWS | The active endpoint `sentiment-analysis-endpoint` was `InService` with one `ml.g5.xlarge` instance and the PyTorch 2.5.1 GPU container with Python 3.11. The active endpoint configuration was `sentiment-analysis-endpoint-media-compat`. The [deployment script](deployment/deploy_endpoint.py) and [inference handler](deployment/inference.py) are in this repository. |
| Invoke the endpoint and inspect its response | One direct invocation verified | A short MELD clip, `dia0_utt0.mp4`, returned predictions for one utterance. The CloudWatch endpoint access log records HTTP 200. The response ranked negative highest for sentiment and surprise highest for emotion. This demonstrates an AWS inference request on a short sample. |
| Build the Next.js workflow for sign-in, API keys, quotas, and uploads | Verified locally against the live endpoint | A test user signed in and loaded the dashboard. An authenticated API request signed and uploaded a video to S3, invoked SageMaker, and charged one quota unit. Hosted verification remains pending. |
| Display video-level and per-utterance results | Implemented in the companion app | The UI renders the primary sentiment and emotion, then each spoken segment with its transcript and confidence scores. The tutorial tested a shortened clip after a longer request timed out. This project has verified a short single-utterance clip directly against SageMaker. App-level behavior on multi-utterance and longer clips remains to be measured. |
| Publish the SaaS and connect its production database | Pending verification | The companion repository uses SQLite in its current Prisma configuration. There is no confirmed hosted release, production database migration, or end-to-end acceptance run in the project evidence reviewed for this report. |

### Offline test metrics recorded by the training job

| Metric | Result |
| --- | ---: |
| Sentiment accuracy | 66.17% |
| Emotion accuracy | 47.70% |
| Sentiment weighted precision | 66.82% |
| Emotion weighted precision | 60.96% |
| Combined test loss | 2.5748 |

The [training script](training/train.py) evaluates the in-memory model after the last epoch. It saves the best validation checkpoint separately. These numbers describe the logged test run. Evaluating the exact checkpoint served by the endpoint and measuring accuracy from production feedback remain separate tasks.

### Deployment work completed by the team

- Fixed a Whisper startup failure by constraining Setuptools to a release that still provides `pkg_resources`.
- Fixed an inference failure with a six-channel audio track by normalizing the stream to mono at 16 kHz. The handler also cleans up each request’s temporary files.
- Verified a successful invocation on the resulting `media-compat` endpoint configuration. Its model archive is `s3://aspect-sentiment-lab/inference/sentiment-analysis-model-media-compat/model.tar.gz`.

## Production extensions beyond the tutorial

| Area | Implemented locally | Live status |
| --- | --- | --- |
| Inference instrumentation | [Structured CloudWatch EMF events](deployment/telemetry.py) for inference timings, classes, confidence, video and audio statistics, speech, and errors. Each successful request contributes predictions from one sampled utterance. Endpoint, model version, and class are the metric dimensions. Correlation identifiers use opaque values or HMAC pseudonyms in the event body. | The currently deployed endpoint has yet to be verified emitting this schema. |
| MELD baseline | [Baseline builder](deployment/build_baseline.py) for class frequencies, confidence and input-stat histograms, and the mean and covariance of the fused embedding, stored as model-versioned JSON. | Baseline build and upload for the served checkpoint are pending. |
| Capture and analytics | Configurable SageMaker input capture at 5% by default, CloudWatch Logs delivery through Amazon Data Firehose to date- and hour-partitioned S3, and [Athena examples](monitoring/queries.sql) for per-user and per-tenant analysis. | The live endpoint configuration showed no data capture. The endpoint log group showed no subscription filters. |
| Drift detection | [Hourly Lambda](monitoring/runtime/drift.py) with PSI for predicted classes and input features, diagonal Mahalanobis distance for embeddings, and a confidence-drop check. Publishes `DriftScore`, `ConfidenceMean`, and `NoSpeechRate`. | No hourly run against live project data has been verified. |
| Alarms and notifications | [CDK stack](monitoring/app.py) defines alarms for 5XX, p95 and p99 model latency, GPU memory, throttling, drift, confidence, and no-speech rate. Composite alarms route notifications through SNS and reference the [runbook](monitoring/RUNBOOK.md). | The monitoring stack and notification delivery are pending. |
| Scaling and cost | The stack configures invocation-based autoscaling, log retention, and S3 lifecycle rules. The [Sydney cost estimate](monitoring/COST.md) is about US$110–145/month for monitoring at one million requests/day, excluding GPU inference and app hosting. It also compares real-time, asynchronous, and serverless inference. | The live endpoint had one provisioned GPU and no registered autoscaling target. Throughput, quota headroom, and actual cost at the proposed traffic level remain unmeasured. |
| User feedback and rolling accuracy | The companion app has an authenticated feedback API. The stack defines DynamoDB storage and an [hourly accuracy publisher](monitoring/runtime/accuracy.py). | The feedback path still needs an end-to-end check. The app has yet to expose a feedback form. |
| Dashboard | The [CDK stack](monitoring/app.py) defines a CloudWatch dashboard for traffic, latency, errors, GPU usage, confidence, class distribution, drift, and feedback accuracy. | Live dashboard data is pending stack deployment and traffic through the instrumented endpoint. |

During the monitoring implementation, the four drift math tests, CDK synthesis, and SaaS TypeScript check passed locally. The deployment audit found no active CloudFormation stack for this monitoring setup in the selected Region.

## Presenting Aspect Sentiment

> Aspect Sentiment analyzes spoken segments in video using audio, visual, and text signals. It displays a video summary and confidence scores for sentiment and emotion in each spoken segment. Our team deployed the model on SageMaker, resolved packaging and multichannel audio failures, and implemented monitoring, drift detection, and a feedback API for a subsequent rollout.

The **Aspect Sentiment** name appears on the dashboard, sign-in and sign-up screens, browser metadata and favicon, both repository READMEs, and the monitoring documents. The active AWS endpoint and the app’s API routes retain their existing identifiers so clients can continue using them.

The current model predicts sentiment and emotion for each spoken segment and the app summarizes those predictions at video level. Aspect-target sentiment, such as scoring “picture quality” and “battery life” independently within the same utterance, would require identifying each target and assigning it its own score. That capability is outside the current implementation.

Credit the starting model and SaaS implementation to Andreas Trolle and describe the team’s deployment, compatibility fixes, and production monitoring work separately. The source repositories retain their MIT license and attribution. [Tutorial](https://youtu.be/Myo5kizoSk0) · [Model license](LICENSE.MD)

## Remaining verification

1. Complete a browser demo from self-service registration to displayed results with a short multi-utterance video. Verify the hosted app and its persistent database when a deployment target is available.
2. Evaluate the exact deployed checkpoint on the held-out MELD split and build the baseline with the same checkpoint, preprocessing settings, and model version.
3. Roll out the monitoring stack and instrumented endpoint configuration in the project. Confirm sampled capture, Firehose delivery, Athena queries, the hourly drift metrics, dashboard data, and SNS delivery.
4. Submit an authenticated correction and confirm it appears in the rolling accuracy metric. Load-test realistic video lengths and concurrent requests before stating a capacity target or deciding whether to add asynchronous inference.
