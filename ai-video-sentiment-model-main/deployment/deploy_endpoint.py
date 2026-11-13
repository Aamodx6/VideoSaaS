"""Create the SageMaker real-time endpoint that serves the sentiment model.

    python deployment/deploy_endpoint.py

PREREQUISITE: whoever runs this needs iam:PassRole on the deploy role.
Verified on this account: the user `aspect-sentimental-frontead-user` does NOT
have it, so CreateModel fails with AccessDeniedException. Run this as a
principal that can pass the role, or grant iam:PassRole to the deploy identity.

Design note -- why this does not use entry_point/source_dir:

Passing those to PyTorchModel makes the SDK *repack* the model. It downloads
the 505 MB artifact to this machine, injects the code, and re-uploads it.
Over a normal connection that takes many minutes and can fail outright.
Instead the code is uploaded once as a small tarball and the container is
pointed at it with SAGEMAKER_SUBMIT_DIRECTORY, so SageMaker fetches both
artifacts inside AWS.

Cost warning: a GPU real-time endpoint has no idle state. It bills from
creation until you delete it:

    aws sagemaker delete-endpoint --endpoint-name sentiment-analysis-endpoint
"""

import io
import os
import sys
import tarfile
from pathlib import Path

import boto3
import sagemaker
from sagemaker.pytorch import PyTorchModel

REGION = "ap-southeast-2"
ACCOUNT = "887885502326"
BUCKET = "aspect-sentiment-lab"
MODEL_URI = f"s3://{BUCKET}/models/model.tar.gz"
CODE_KEY = "inference/sourcedir.tar.gz"
CODE_URI = f"s3://{BUCKET}/{CODE_KEY}"

ENDPOINT_NAME = os.environ.get("ENDPOINT_NAME", "sentiment-analysis-endpoint")
INSTANCE_TYPE = os.environ.get("ENDPOINT_INSTANCE_TYPE", "ml.g5.xlarge")

# Set SAGEMAKER_ROLE_ARN if the deploy role is named differently. The default
# matches the role in the architecture diagram.
ROLE_ARN = os.environ.get(
    "SAGEMAKER_ROLE_ARN",
    f"arn:aws:iam::{ACCOUNT}:role/sentiment-analysis-deploy-role",
)

# Absolute path to this directory. A relative source_dir would resolve against
# the current working directory, so running from the repo root would package
# the whole repository -- including dataset/ and venv/.
HERE = Path(__file__).resolve().parent
SKIP_DIRS = {"__pycache__", ".ipynb_checkpoints"}
SKIP_SUFFIX = {".pyc", ".pyo"}


def build_code_tarball():
    """Flat tarball with inference.py at the root, as the container expects."""
    buf = io.BytesIO()
    count = 0
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path in sorted(HERE.rglob("*")):
            rel = path.relative_to(HERE)
            if any(part in SKIP_DIRS for part in rel.parts):
                continue
            if path.suffix in SKIP_SUFFIX or not path.is_file():
                continue
            tar.add(path, arcname=str(rel).replace("\\", "/"))
            count += 1
    data = buf.getvalue()
    print(f"Code     : {len(data):,} bytes ({count} files)")
    return data


def upload_code(s3):
    s3.put_object(Bucket=BUCKET, Key=CODE_KEY, Body=build_code_tarball())
    print(f"           {CODE_URI}")


def require_artifact(s3):
    """Fail early, and show exactly which bytes are about to be served."""
    bucket, _, key = MODEL_URI.replace("s3://", "").partition("/")
    try:
        head = s3.head_object(Bucket=bucket, Key=key)
    except Exception as exc:
        sys.exit(
            f"Cannot read {MODEL_URI}: {exc}\n"
            "Train a model, or run promote_on_complete.py, before deploying."
        )
    print(f"Artifact : {MODEL_URI}")
    print(f"           {head['ContentLength']:,} bytes, "
          f"modified {head['LastModified']}")
    return head


def deploy_endpoint():
    s3 = boto3.client("s3", region_name=REGION)
    require_artifact(s3)
    upload_code(s3)
    print(f"Role     : {ROLE_ARN}")
    print(f"Instance : {INSTANCE_TYPE}")

    # default_bucket pins SDK artifacts to the project bucket. Left unset, the
    # SDK falls back to sagemaker-ap-southeast-2-887885502326.
    session = sagemaker.Session(
        boto_session=boto3.Session(region_name=REGION),
        default_bucket=BUCKET,
    )

    model = PyTorchModel(
        model_data=MODEL_URI,
        role=ROLE_ARN,
        framework_version="2.5.1",
        py_version="py311",
        sagemaker_session=session,
        env={
            "SAGEMAKER_SUBMIT_DIRECTORY": CODE_URI,
            "SAGEMAKER_PROGRAM": "inference.py",
        },
    )

    try:
        predictor = model.deploy(
            initial_instance_count=1,
            instance_type=INSTANCE_TYPE,
            endpoint_name=ENDPOINT_NAME,
        )
    except Exception as exc:
        text = str(exc)
        if "PassRole" in text:
            sys.exit(
                f"Deploy blocked: this identity cannot pass {ROLE_ARN}.\n"
                "Run as a principal with iam:PassRole, or grant it to this user. "
                "Nothing was created."
            )
        raise

    print(f"Endpoint ready: {ENDPOINT_NAME}")
    print("Delete it when idle; it bills continuously.")
    return predictor


if __name__ == "__main__":
    deploy_endpoint()
