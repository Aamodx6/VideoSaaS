# Aspect Sentiment monitoring runbook

## Serving

Open the endpoint dashboard and compare traffic, 5XX rate, p95/p99 latency and GPU memory. Check SageMaker endpoint status, current capacity and scaling activities. Look for finite `error_type` values and compare the frame, audio, transcription/text and fusion latencies to locate the slow stage. A throttling incident requires checking the caller's CloudWatch EMF delivery, SDK retries and SageMaker quotas. Raise the capacity ceiling only after confirming quota and load-test results. For sustained regressions, roll back to the previously verified endpoint configuration and model version.

## Drift

Check `DriftSampleCount` and baseline coverage before interpreting a signal. Read the drift Lambda event for the component PSI or embedding shift driving the score. Compare like model versions and review clip length, audio levels and speech presence. MELD consists of short utterance clips, so long or silent user videos can cause a persistent domain shift. Review feedback counts and both label accuracies before deciding whether to recalibrate the baseline, adjust the upload policy, or retrain. Feedback accuracy is conditional on submitted labels.

## Pipeline

Check the Firehose delivery stream, source subscription, the normalizer's logs and `firehose-errors/`. Verify the prefix in S3 for the affected UTC hour and check Lambda permissions, memory, timeouts and the deployed version's immutable baseline. Confirm the Athena date and hour filters cover Firehose arrival time. If one drift window missed late records, wait for S3 delivery and replay the original EventBridge `time` into the drift Lambda. Check SNS subscription confirmation and composite alarm history if notifications are missing.
# Input quality and moderation

## Input quality

Check the serving model version and policy artifact if OOD or degraded input rises. Compare confidence, entropy, and held-out quality scores against the previous release. Verify that the moderation workflow, GuardDuty scans, and managed job completions are healthy if block rates move unexpectedly. Inspect aggregate counts and reason codes. Keep transcripts and unsafe media out of operational logs and training datasets. Leave ambiguous uploads in review pending authorized disposition.
