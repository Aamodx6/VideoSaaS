# Whole-project verification

## Guardrail branch validation

Local guardrail verification covers 27 Python tests, eight frontend contract tests, the TypeScript check, a production Next.js build, and PostgreSQL tests for safety-role permissions and subject isolation. CDK synthesis includes the scan workflow, recovery, safety database secret, and the guarded API. Managed AWS scans, real model-policy fitting, and the hosted moderation flow need staging resources before live validation. These local results use mocked managed-service calls.

## Passed

- Production Next.js build completed. The browser test initially hit Auth.js `UntrustedHost`. Setting `AUTH_URL` to the canonical origin fixed signup and sign-in. The environment template now documents this setting.
- Chrome exercised signup, API key creation, session persistence, the dashboard, and rejection of an unauthenticated upload request.
- Chrome uploaded a MELD clip to S3, invoked the live Sydney real-time SageMaker endpoint, and displayed the prediction. SQLite recorded one quota use and marked the video analyzed. Temporary test records and the upload were removed.
- A separate request to the real-time endpoint returned HTTP 200 and one utterance.
- The earlier local pass covered CDK synthesis with the async endpoint enabled, eight template assertions, seven PostgreSQL isolation and erasure assertions, ten mocked AWS scenarios, and four drift tests.
- The training logging smoke test passed under Python 3.11 with CPU PyTorch 2.5.1 and the pretrained BERT and R3D-18 weights. The test fixture now provides the labeled dictionaries the trainer expects. This test exercises model construction and metric logging.
- The revised CDK staging template synthesized and passed Sydney CloudFormation lint with warnings about explicit Availability Zones and redundant generated dependencies. AWS lists Aurora PostgreSQL 16.15 as orderable for Serverless in Sydney. The retired 16.6 default was replaced.

## Remaining

- AWS inventory for console dot dream showed the real-time endpoint in service, no async endpoint, and no active CloudFormation stacks in Sydney. Live tests for Aurora and RDS Proxy, secret rotation, async delivery and DLQ, Valkey, monitoring, and disaster recovery need a deployed staging stack.
- The project is on the Free plan. AWS reports a quota of two `ml.g5.xlarge` endpoint instances, with one in use by the real-time endpoint. Limit staging to one additional GPU. A hosted SaaS workload role is still needed for the CDK grants. AWS service-side template validation hit the request-size limit, while local CloudFormation lint completed.
- Full model retraining and regression coverage remain pending. The smoke test exercised trainer initialization and metric logging, and live inference exercised the deployed checkpoint.
- Capture and Parquet publishers, direct user notification delivery, and the hosted dashboard’s PostgreSQL and async integration remain unfinished. Load-test the completed staging system before making a one-million-requests-per-day capacity claim.
