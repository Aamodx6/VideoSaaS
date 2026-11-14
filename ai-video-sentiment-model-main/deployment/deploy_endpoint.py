import os
from pathlib import Path

from sagemaker.model_monitor import DataCaptureConfig
from sagemaker.pytorch import PyTorchModel
import sagemaker
from telemetry import DIMENSION_VALUE


def deploy_endpoint():
    if os.getenv("REQUIRE_QUALITY_POLICY", "1") == "1" and not os.getenv("QUALITY_POLICY_PATH"):
        raise ValueError("Set QUALITY_POLICY_PATH to the calibrated artifact path before deploying")
    bucket = os.environ["INFERENCE_BUCKET"]
    endpoint = os.environ["ENDPOINT_NAME"]
    version = os.environ["MODEL_VERSION"]
    if not DIMENSION_VALUE.fullmatch(version):
        raise ValueError("MODEL_VERSION must be a short alphanumeric identifier")
    sample_rate = float(os.getenv("DRIFT_SAMPLE_RATE", "0.05"))
    if not 0 <= sample_rate <= 1:
        raise ValueError("DRIFT_SAMPLE_RATE must be between 0 and 1")
    capture = int(os.getenv("CAPTURE_PERCENT", "5"))
    if not 0 <= capture <= 100:
        raise ValueError("CAPTURE_PERCENT must be between 0 and 100")
    artifact_prefix = os.getenv("MODEL_ARTIFACT_PREFIX", "inference/artifacts/").strip("/")
    session = sagemaker.Session(default_bucket=bucket, default_bucket_prefix=artifact_prefix)
    capture_config = None
    if capture:
        capture_config = DataCaptureConfig(
            enable_capture=True,
            sampling_percentage=capture,
            destination_s3_uri=os.environ["CAPTURE_S3_URI"],
            capture_options=["REQUEST"],
            json_content_types=["application/json"],
            sagemaker_session=session,
        )
    role = os.environ["SAGEMAKER_EXECUTION_ROLE_ARN"]

    model_uri = os.getenv("MODEL_S3_URI", f"s3://{bucket}/{artifact_prefix}/model.tar.gz")

    model = PyTorchModel(
        model_data=model_uri,
        role=role,
        framework_version="2.5.1",
        py_version="py311",
        entry_point="inference.py",
        source_dir=str(Path(__file__).resolve().parent),
        name=os.environ["SAGEMAKER_MODEL_NAME"],
        env={
            "PIP_CONSTRAINT": "/opt/ml/model/code/build-constraints.txt",
            "PIP_BUILD_CONSTRAINT": "/opt/ml/model/code/build-constraints.txt",
            "ENDPOINT_NAME": endpoint,
            "MODEL_VERSION": version,
            "DRIFT_SAMPLE_RATE": str(sample_rate),
            "QUALITY_POLICY_PATH": os.getenv("QUALITY_POLICY_PATH", ""),
            "REQUIRE_QUALITY_POLICY": os.getenv("REQUIRE_QUALITY_POLICY", "1"),
            "INFERENCE_BUCKET": bucket,
            "INFERENCE_INPUT_PREFIX": os.getenv("INFERENCE_INPUT_PREFIX", "inference/uploads/"),
        },
        sagemaker_session=session,
    )

    model.deploy(
        initial_instance_count=int(os.getenv("INITIAL_INSTANCE_COUNT", "1")),
        instance_type=os.getenv("INSTANCE_TYPE", "ml.g5.xlarge"),
        endpoint_name=endpoint,
        update_endpoint=os.getenv("UPDATE_ENDPOINT", "false").lower() == "true",
        data_capture_config=capture_config,
        container_startup_health_check_timeout=int(os.getenv("STARTUP_TIMEOUT_SECONDS", "600")),
    )


if __name__ == "__main__":
    deploy_endpoint()
