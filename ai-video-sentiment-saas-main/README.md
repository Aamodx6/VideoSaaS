# Aspect Sentiment · SaaS

Upload a video to review the predicted sentiment and emotion in each spoken segment. The dashboard displays a video summary, timestamps, transcribed text, and confidence scores. The API uses per-user keys and usage quotas, and sends inference requests to the Aspect Sentiment model on SageMaker.

The starting implementation follows [Andreas Trolle’s tutorial](https://youtu.be/Myo5kizoSk0). The upstream copyright and license are retained in [LICENSE.MD](LICENSE.MD).

In the two-repository workspace, see the [project guide](../README.md), [system architecture](../ARCH.md), and [hosting checklist](../HOSTING.md).

## Setup

Follow these steps to install and set up the SaaS project:

### Installation

1. Open the SaaS repository.

```bash
cd ai-video-sentiment-saas
```

2. Install dependencies

```
npm install
```

3. Copy `.env.example` to `.env` and set the database, Auth.js and [AWS serving configuration](#aws-serving-configuration). Use the AWS SDK credential chain for access.

```
DATABASE_URL="your-database-url"
AUTH_SECRET="your-auth-secret"
AUTH_URL="http://localhost:3000"
AWS_REGION="your-aws-region"
AWS_INFERENCE_BUCKET="your-inference-bucket"
AWS_ENDPOINT_NAME="your-sagemaker-endpoint"
```

4. Initialize the database:

```
npm run db:migrate
```

## Running the app

### Development

```
npm run dev
```

### Production

```
npm run build
npm start
```

## AWS serving configuration

For the existing Aspect Sentiment deployment in **console dot dream**, use region `ap-southeast-2`, bucket `aspect-sentiment-lab`, prefix `inference/uploads/`, and endpoint `sentiment-analysis-endpoint`. Keep the local SQLite database and Auth.js secret in your ignored development configuration. A shared hosted deployment needs a persistent database and a server-side AWS workload identity.

Set `AWS_REGION`, `AWS_INFERENCE_BUCKET`, `AWS_INFERENCE_INPUT_PREFIX`, and `AWS_ENDPOINT_NAME` from the endpoint deployment. The AWS SDK uses the default credential chain. Use a profile for development or an IAM workload role scoped to the configured S3 upload prefix, endpoint, and feedback table. Forward the application's unmodified JSON stdout to standard CloudWatch Logs to activate the throttle metric. Set `MODEL_VERSION` to the active version, and store independent random `TELEMETRY_HMAC_SECRET` and `FEEDBACK_SIGNING_SECRET` values in the hosting environment's secret store. Set `AWS_FEEDBACK_TABLE` to the CDK output and keep feedback retention longer than the accuracy window.

The sentiment inference API returns a signed `feedback_token` on each labeled utterance when feedback is configured and the endpoint returns its model version. Send it to `POST /api/feedback` with the same bearer API key:

```json
{
  "token": "<feedback_token>",
  "correct_emotion": "neutral",
  "correct_sentiment": "negative"
}
```

Successful submissions return HTTP 204. Tokens expire after seven days and can be used only once by the original caller. Feedback stores labels and the model version, and the hourly accuracy job uses sharded counters updated atomically with each accepted label.
