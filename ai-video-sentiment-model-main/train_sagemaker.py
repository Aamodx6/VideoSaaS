import sys
from urllib.parse import urlparse

import boto3
from sagemaker.pytorch import PyTorch
from sagemaker.debugger import TensorBoardOutputConfig

# The SDK streams container stdout straight to the console. Training logs
# contain tqdm progress bars, which are non-ASCII, and a Windows console is
# cp1252 by default -- so the first progress bar raises UnicodeEncodeError
# inside sagemaker/logs.py and kills this process mid-fit(). The job itself
# survives (AWS runs it independently), but fit() never returns, so
# promote_model_artifact() below never runs. Degrade unencodable characters
# instead of crashing.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")

# Curated copy that deployment/deploy_endpoint.py reads.
MODEL_ARTIFACT_URI = "s3://aspect-sentiment-lab/models/model.tar.gz"
REGION = "ap-southeast-2"


def promote_model_artifact(estimator, destination=MODEL_ARTIFACT_URI, region=REGION):
    """Copy the finished job's model.tar.gz over the curated deployment copy.

    Training writes to a job-scoped prefix
    (training-output/<job-name>/output/model.tar.gz) and nothing updates
    models/ automatically, so without this step deployment silently keeps
    shipping whatever checkpoint was last copied there by hand.
    """
    source = estimator.model_data
    if not source:
        print("No model artifact produced; skipping promotion to models/.")
        return None

    src = urlparse(source)
    dst = urlparse(destination)
    if not src.netloc or not src.path.startswith("/"):
        raise ValueError(f"Unparseable model artifact URI: {source!r}")

    boto3.client("s3", region_name=region).copy_object(
        Bucket=dst.netloc,
        Key=dst.path.lstrip("/"),
        CopySource={"Bucket": src.netloc, "Key": src.path.lstrip("/")},
    )
    print(f"Promoted {source} -> {destination}")
    return destination


def start_training():
    tensorboard_config = TensorBoardOutputConfig(
        s3_output_path="s3://aspect-sentiment-lab/tensorboard",
        container_local_output_path="/opt/ml/output/tensorboard"
    )

    estimator = PyTorch(
        entry_point="train.py",
        source_dir="training",
        role="arn:aws:iam::887885502326:role/aspect-sentiment-executional-role",
        framework_version="2.5.1",
        py_version="py311",
        instance_count=1,
        instance_type="ml.g5.xlarge",
        output_path="s3://aspect-sentiment-lab/training-output",
        code_location="s3://aspect-sentiment-lab/code",
        region_name="ap-southeast-2",
        hyperparameters={
            "batch-size": 32,
            "epochs": 25
        },
        tensorboard_output_config=tensorboard_config
    )

    estimator.fit({
        "training": "s3://aspect-sentiment-lab/dataset/train",
        "validation": "s3://aspect-sentiment-lab/dataset/dev",
        "test": "s3://aspect-sentiment-lab/dataset/test"
    })

    promote_model_artifact(estimator)


if __name__ == "__main__":
    start_training()
