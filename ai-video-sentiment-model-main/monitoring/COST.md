# Aspect Sentiment monitoring cost and inference options

## Moderation cost at one million uploads per day

Budget moderation separately from inference and monitoring. At 30 million uploads per 30-day month, the six-image first pass uses up to 180 million Rekognition image calls before early exits. Full-video usage is `upload count × borderline fraction × average borderline clip minutes`. Transcribe usage is `eligible upload count × average billable audio duration`, including any per-request minimum. GuardDuty scales with scanned bytes and object count. Step Functions usage scales with polling transitions and retries. Quarantine storage scales with daily uploaded bytes, retention and the extra checked copies. Apply Sydney list rates and measure the escalation fraction in staging before extrapolating. [Rekognition pricing](https://aws.amazon.com/rekognition/pricing/) · [Transcribe pricing](https://aws.amazon.com/transcribe/pricing/) · [GuardDuty pricing](https://aws.amazon.com/guardduty/pricing/) · [Step Functions pricing](https://aws.amazon.com/step-functions/pricing/)

## Lowest-cost demo and staging

- Use the local Next.js app and SQLite for demos. Keep the full data stack opt-in. Aurora with a failover reader and proxy, Valkey, and interface endpoints add recurring charges even at low traffic.
- Keep the model artifact and endpoint configuration for recreation. When no demo or integration run is scheduled, deleting the real-time endpoint stops the provisioned GPU charge and temporarily disables the live inference path. Verify the endpoint is gone and check usage in AWS Settings → Billing.
- When an always-available submission API is needed, use the async endpoint with `ASYNC_MIN_INSTANCES=0` and `ASYNC_MAX_INSTANCES=1` for staging. The backlog alarm adds one instance when queued work has no capacity. Allow for a GPU cold start and verify a real wake-and-complete cycle in Sydney before switching clients.
- For large, sustained traffic, benchmark supported GPU types and clip lengths using cost per successful prediction. Keep short video retention and measure actual upload size before estimating storage.

Planning estimate for Sydney, in USD for a 30-day month at 1 million requests/day. These are rounded unit assumptions for budgeting; use the AWS regional price selector and measured log sizes before launch.

| Component | Monthly assumptions | Estimate |
| --- | --- | ---: |
| CloudWatch Logs | 30M events × 1.8 KB, plus 5% × 2.2 KB for sampled vectors, plus the normalized copy; about 107 GiB total at a budget rate of $0.76/GiB | $81 |
| Firehose | 15–145 GiB after source batching, compression and 5 KiB per-record rounding, at $0.04/GiB | $1–6 |
| S3 | About 15 GiB compressed at steady state plus captures and S3 requests | $2–5 |
| Lambda, EventBridge and SNS | Batched normalization and 720 runs of each scheduled job, with normal alarm volume | $2–9 |
| Metrics, alarms and dashboard | Allowance for one endpoint and model version, approximately 40 custom series, 15 alarms and one dashboard | $17–26 |
| Athena | Up to 0.25 TB scanned/month using time partition filters | $1–2 |
| DynamoDB | 1% feedback participation, two transactional writes per response, hourly counter reads, TTL and PITR | $4–12 |
| **Total** | Before taxes, discounts and free allowances | **about $110–145/month** |

Measure `IncomingBytes` on both log groups and Firehose record/byte counts. Extra container stdout, high error volume, longer retention, multiple model versions and more feedback increase the estimate. The estimate excludes SageMaker GPU time, original video storage, the app/database, and inter-region traffic. The minimum real-time GPU adds roughly `730 × regional ml.g5.xlarge hourly price` each month; during rolling updates plan for two fleets temporarily. Check spend in AWS Settings → Billing.

| Mode | Fit | Cost and integration |
| --- | --- | --- |
| Real-time GPU (current) | Synchronous short clips, model processing limit of 60 seconds | Pays for provisioned instances, including idle time. The app keeps its current API contract and this stack monitors the real-time endpoint. |
| Asynchronous GPU | Long videos or bursty jobs, up to one hour per request | Queue requests, track job IDs and read results from S3. Idle compute can scale to zero with backlog and wake-from-zero policies, at the cost of cold starts, queue delay and storage. At sustained traffic, GPU savings depend on utilization and concurrency. |
| Serverless inference | Short CPU models fitting the memory and latency limits | Charges for execution and optionally provisioned concurrency. No GPU, no SageMaker data capture, and the same 60-second limit. This model needs a CPU throughput/memory benchmark and separate capture design before considering it. |

AWS references: [CloudWatch pricing](https://aws.amazon.com/cloudwatch/pricing/), [Firehose pricing](https://aws.amazon.com/firehose/pricing/), [S3 pricing](https://aws.amazon.com/s3/pricing/), [Lambda pricing](https://aws.amazon.com/lambda/pricing/), [Athena pricing](https://aws.amazon.com/athena/pricing/), [DynamoDB pricing](https://aws.amazon.com/dynamodb/pricing/), [SageMaker pricing](https://aws.amazon.com/sagemaker/ai/pricing/), [inference modes](https://docs.aws.amazon.com/sagemaker/latest/dg/deploy-model-options.html), [async scaling](https://docs.aws.amazon.com/sagemaker/latest/dg/async-inference-autoscale.html).
