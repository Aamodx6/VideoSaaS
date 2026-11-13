"""Wait for a SageMaker training job, then promote its model.tar.gz to models/.

Why this exists: the launcher that called fit() died on a Windows console
encoding error, so its post-fit() promote_model_artifact() call will never
run. Rather than rerun a 12-hour job to regain that one line, this watches the
already-running job and performs the same copy when it completes.

Nothing here touches the job. It only polls DescribeTrainingJob and then does
a server-side S3 copy, so it cannot affect training in any way.

Usage:
    python promote_on_complete.py [job-name]

Default job name is the run that was in flight when the launcher crashed.
"""

import sys
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import boto3

from train_sagemaker import MODEL_ARTIFACT_URI, REGION, promote_model_artifact

DEFAULT_JOB = "pytorch-training-2026-11-10-04-34-48-919"
TERMINAL = {"Completed", "Failed", "Stopped"}


def stamp():
    return datetime.now(timezone.utc).strftime("%H:%M:%SZ")


def watch(job, region=REGION, poll_seconds=120):
    client = boto3.client("sagemaker", region_name=region)
    s3 = boto3.client("s3", region_name=region)

    last = None
    while True:
        try:
            d = client.describe_training_job(TrainingJobName=job)
        except Exception as exc:  # transient API error: retry rather than die
            print(f"[{stamp()}] describe failed ({exc}); retrying", flush=True)
            time.sleep(poll_seconds)
            continue

        status = d["TrainingJobStatus"]
        secondary = d.get("SecondaryStatus", "")
        combined = (status, secondary)
        if combined != last:
            print(f"[{stamp()}] {status} / {secondary}", flush=True)
            last = combined

        if status in TERMINAL:
            return d, s3

        time.sleep(poll_seconds)


def main():
    job = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_JOB
    print(f"Watching {job} in {REGION}; promoting to {MODEL_ARTIFACT_URI}", flush=True)

    detail, s3 = watch(job)
    status = detail["TrainingJobStatus"]

    if status != "Completed":
        reason = detail.get("FailureReason") or "(no failure reason reported)"
        print(f"\nJob ended {status}: {reason}", flush=True)
        print("Not promoting: a failed or stopped job has no trustworthy artifact.", flush=True)
        return 1

    output_path = detail["OutputDataConfig"]["S3OutputPath"].rstrip("/")
    artifact = f"{output_path}/{job}/output/model.tar.gz"
    print(f"\nJob completed. Artifact: {artifact}", flush=True)

    bucket, _, key = artifact.replace("s3://", "").partition("/")
    head = s3.head_object(Bucket=bucket, Key=key)
    size = head["ContentLength"]
    print(f"Verified artifact present: {size:,} bytes", flush=True)

    promote_model_artifact(SimpleNamespace(model_data=artifact))
    print(f"Done. {MODEL_ARTIFACT_URI} is now {size:,} bytes from {job}.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
