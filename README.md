# Aspect Sentiment

Aspect Sentiment analyzes spoken segments in videos using visual, audio, and text features. It returns emotion and sentiment labels with confidence scores and summarizes them in a Next.js dashboard. The current model scores each spoken segment. It does not extract individual topics and score their sentiment separately.

## Project guide

| Document | What it covers |
| --- | --- |
| [Architecture](ARCH.md) | Browser, Next.js backend, PostgreSQL and SQLite, SageMaker, AWS networking, async inference, and observability |
| [Hosting and deployment](HOSTING.md) | Local demo, public hosting choice, credentials, endpoint restoration, and cost controls |
| [Model repository](ai-video-sentiment-model-main/README.md) | MELD, training, model packaging, endpoint deployment, and monitoring |
| [SaaS repository](ai-video-sentiment-saas-main/README.md) | Frontend setup, API routes, database migration, and runtime configuration |
| [Tutorial progress](ai-video-sentiment-model-main/ASPECT_SENTIMENT_PROGRESS.md) | Tutorial sequence, completed milestones, and team contributions |
| [Data architecture](ai-video-sentiment-model-main/monitoring/DATA_ARCHITECTURE.md) | Tenant model, DynamoDB access patterns, retention, erasure, and recovery |
| [Verification report](ai-video-sentiment-model-main/monitoring/TEST_STATUS.md) | Browser, inference, model smoke tests, and staging gaps |
| [Monitoring cost](ai-video-sentiment-model-main/monitoring/COST.md) | Cost assumptions, GPU choices, and optional infrastructure |
| [Monitoring runbook](ai-video-sentiment-model-main/monitoring/RUNBOOK.md) | Alarms, metrics, diagnostics, and recovery |

## Run locally

Use Node.js 22 for the Next.js app and Python 3.11 for the model smoke test. In the SaaS repository, copy `.env.example` to `.env`, generate an `AUTH_SECRET`, and set `AUTH_URL` to the browser origin. Use `DATABASE_URL=file:./db.sqlite` locally.

```powershell
cd ai-video-sentiment-saas-main
npm ci
npm run db:migrate
npm run dev
```

Open [the local app](http://localhost:3000), create an account, and open the dashboard. The live upload path requires an AWS workload session with S3 upload and SageMaker invoke permissions and a running endpoint. The production build uses `npm run build` and `npm start`. If the GPU is off, use a clearly labeled recorded demo until it is restored.

## What has been verified

- The Sydney real-time endpoint was deleted between demos to stop idle GPU charges. The saved configuration, model registration, and S3 archive were verified after deletion. See the [restoration procedure](HOSTING.md#restore-the-real-time-demo).
- The training job completed in Sydney. The team fixed Whisper packaging and normalized multichannel audio for inference.
- A local production browser session passed signup, upload to S3, SageMaker inference, result rendering, and a single quota deduction.
- The model logging smoke test, drift math tests, local PostgreSQL tenant isolation, and CDK synthesis passed. The async stack and shared hosted database still require live staging verification.

The tutorial and starting implementation are credited to Andreas Trolle. Both source repositories retain the original MIT license and copyright notices. [Original tutorial](https://youtu.be/Myo5kizoSk0)
