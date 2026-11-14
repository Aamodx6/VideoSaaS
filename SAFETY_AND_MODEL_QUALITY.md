# Upload safety and model quality

Guardrails are implemented and enabled by default for the optional async stack and the Next.js upload flow. Managed-service validation and model calibration remain deployment steps. Installing the app and running local tests create no AWS resources.

## Moderated upload path

~~~mermaid
flowchart LR
  App[Next.js dashboard] --> API[Signed async API]
  API --> Q[Versioned S3 quarantine]
  Q --> Malware[GuardDuty malware scan]
  API --> Pending[DynamoDB pending job]
  Pending --> Workflow[Step Functions]
  Malware --> Workflow
  Workflow --> Probe[MP4 header and FFprobe]
  Probe --> Frames[Rekognition sampled frames]
  Frames -->|Borderline| Video[Full video moderation]
  Frames --> Language[Audio language identification]
  Language -->|Confident US English| Toxicity[Transcribe toxicity]
  Video --> Policy{PASS REVIEW BLOCK}
  Toxicity --> Policy
  Policy -->|PASS| Processing[Processing bucket and async inference]
  Policy -->|REVIEW| Review[IAM restricted reviewer API]
  Policy -->|BLOCK| Audit[Aurora audit and strikes]
  Processing --> Quality[Calibrated quality and OOD checks]
  Quality --> Result[Prediction or abstention]
~~~

The browser receives a size-limited S3 POST. The job pins the uploaded VersionId. Validation, scans, and promotion use that version, so replacing the object cannot change a pending submission. The validator checks MP4 magic bytes, FFprobe duration and dimensions, and six sampled frames. Borderline image scores trigger full-video moderation. Missing or unsupported scan results enter REVIEW and cannot invoke inference.

Audio first goes through managed language identification. Only confidently identified US English is sent to toxicity detection. Unsupported speech enters review. A video with no audio skips speech moderation, while its visual content must still pass. The model can return audio_unavailable rather than a label. An empty transcript from an audio track does not establish clearance. [Transcribe toxicity support](https://docs.aws.amazon.com/transcribe/latest/dg/toxicity.html)

## Staging setup

1. Confirm Sydney in AWS Settings → View all projects → Overview → Additional Info → Region, and check the billing plan. This experience lists Transcribe and Firehose under its Paid plan. The code does not change the plan. [AWS availability](https://docs.aws.amazon.com/accounts/latest/reference/supported-services-sign-up-new.html)
2. Set ENABLE_DATA_ARCHITECTURE=1. ENABLE_MODERATION defaults to 1 for this extension. Supply the stack's existing model, workload role, origin, and runbook settings.
3. Supply MODERATION_MEDIA_LAYER_ARN for a vetted Linux x86-64 FFmpeg/FFprobe layer with binaries under /opt/bin. Supply MODERATION_REVIEW_ROLE_ARN for an authorized reviewer identity distinct from the app.
4. Create storage and database first. Apply monitoring/sql/001_tenants.sql and 002_strikes.sql as the migration operator. Bootstrap the aspect_app and aspect_safety passwords from the generated secrets through a trusted database client. Then set ENABLE_DATABASE_ROTATION=1 to create their rotation schedules. The safety role accesses only its strike/audit tables. Test RDS Data API access and rotation before accepting uploads.
5. Build quality-policy.json with the released model and independent MELD splits. Package it in the serving archive and set MODEL_VERSION and QUALITY_POLICY_PATH=/opt/ml/model/quality-policy.json. REQUIRE_QUALITY_POLICY defaults to 1 in the endpoint launchers. A missing path rejects deployment before creating serving capacity.
6. Stage the archive, enable CREATE_ASYNC_ENDPOINT=1, and test real managed scans, REVIEW, BLOCK, cleanup, and cold-start inference.
7. Configure the host with AWS_JOBS_API_URL, AWS_API_KEYS_TABLE, REQUIRE_MODERATION=1, PostgreSQL URLs, Auth.js secrets, and the authorized app workload identity. Verify S3 CORS for APP_ORIGIN.

The app validates existing Prisma keys and enrolls their SHA-256 digests in DynamoDB. SHA-256-derived opaque subject IDs represent the user in S3 and safety records. The current app has one tenant per user. Existing readable Prisma keys remain a dashboard compatibility dependency. Digest registration does not erase their original database representation. Registry records expire after 90 days, and expired/revoked keys require administrative rotation or renewal.

The async artifact must contain model.pth, quality-policy.json, and code/ with the deployment inference handler, models, quality helpers, telemetry, requirements, and build constraints. The async container explicitly uses code/inference.py. Changing the archive alone requires a new endpoint configuration and a verified rollout.

## Configuration

| Setting | Default and behavior |
| --- | --- |
| REQUIRE_MODERATION | Enabled unless explicitly 0 for a private legacy demo |
| ENABLE_MODERATION | 1 for the CDK data extension |
| MAX_VIDEO_BYTES | 128 MiB at signing and submission |
| MAX_VIDEO_SECONDS | 120 |
| MODERATION_REVIEW_SCORE | 65 out of 100 |
| MODERATION_BLOCK_SCORE | 90 out of 100 |
| QUARANTINE_RETENTION_DAYS | 2 for pending/review objects |
| BLOCK_RETENTION_DAYS | 0, delete working content after terminal audit |
| STRIKE_THROTTLE_COUNT | 3 active strikes |
| STRIKE_SUSPEND_COUNT | 5 active strikes |
| REQUIRE_QUALITY_POLICY | 1 in the deployment launchers |
| DIAGNOSTICS_TOKEN | At least 32 characters to authorize the Neon status endpoint |

Dimensions are bounded at 3840×2160. The browser conservatively enforces 128 MiB even if a larger backend bound is configured.

## API and account enforcement

The dashboard requests /api/async-inference/uploads, sends returned fields and the file as an S3 multipart form, submits upload_id to /api/async-inference/jobs, and polls /api/async-inference/jobs/{jobId}. Results are fetched through the authenticated API. REVIEW displays a job reference. A submission consumes one monthly quota unit even if subsequently blocked.

GET /api/async-inference/quota reads current UTC-month usage from DynamoDB. The dashboard refreshes this counter after submission rather than displaying the legacy PostgreSQL counter.

| Code | HTTP status and meaning |
| --- | --- |
| MODERATED_UPLOAD_REQUIRED | 403, synchronous demo path disabled |
| ACCOUNT_SUSPENDED | 403 before accepting new work, or a blocked job if suspended while queued |
| INVALID_MEDIA | 422, invalid container, duration, or dimensions |
| MALWARE_DETECTED | 422, managed threat detection |
| CONTENT_POLICY | 422, automated moderation blocked the upload |
| HUMAN_REVIEW_BLOCK | 422, reviewer rejection |
| SAFETY_CHECK_UNAVAILABLE | 503, account policy cannot be established |
| REVIEW_MEDIA_EXPIRED | 410 from reviewer approval after quarantine expiry |

The worker requires moderation_decision=PASS and rechecks suspension before submitting to SageMaker. Monthly quota reservation and job creation use one DynamoDB transaction. Quota decisions never query Aurora. The API checks Aurora strikes before new uploads/submissions, with a reduced limit of five requests per minute after throttling. Existing results remain readable.

Aurora stores idempotent moderation audit events and strikes. The first content-policy strike warns, three throttle, and five suspend. Invalid media or unavailable services do not add strikes. Strikes expire after 90 days and audit events after 365 days. A daily job purges expired rows. SQL operations use a dedicated Secrets Manager identity limited to the safety tables.

## Reviewer workflow

The reviewer API uses IAM and a resource policy restricted to MODERATION_REVIEW_ROLE_ARN.

- GET /review/{tenant}?after=<cursor> lists up to 25 job IDs through ByReviewTenant.
- GET /review/{tenant}/{jobId} returns the reason and a five-minute version-specific review URL.
- POST to that route accepts a JSON decision of PASS or BLOCK.
- Approval requires completed managed scans and a borderline content result. It cannot override malware, invalid media, absent scans, or unsupported language.
- A conditional decision claim prevents conflicting reviewers from committing different outcomes. Same-decision retries can complete audit and copying without a second strike.

## Retention and reporting

Terminal PASS and BLOCK remove all working upload, checked-copy, transcript, and delete-marker versions after durable audit. PASS first copies the approved version into processing storage. Copy fingerprints make retries safe after the original is erased.

BLOCK_RETENTION_DAYS>0 preserves an encrypted private copy under hold/ before removing working copies. Its lifecycle is separate from pending/review content. This is operational retention. A formal legal hold requires a governed immutable store and authorized disposition process. Noncurrent quarantine versions expire after one day. S3 lifecycle expiration is eventual.

The erasure helper now covers quarantine, checked/retained copies, language/toxicity files, and Aurora safety rows. It refuses deletion while moderation or inference is active. Use the opaque IDs registered for the user. Replay the deletion ledger before restoring access from backups. Completed Transcribe jobs are removed where supported; managed service retention also follows AWS terms.

A vetted hash-matching integration belongs before final disposition, with missing clearance keeping the job in review. Legal reporting remains an authorized operator workflow with opaque object/version references. Customer moderation media and blocked files are excluded from calibration and training. Review AWS AI service data-use settings before processing customer content.

## Calibration and abstention

The handler computes sampled blur, face visibility, silence, a narrow-band tone proxy for bleeps, Whisper segment confidence, calibrated confidence, entropy, and regularized full-covariance Mahalanobis distance. Tone and face measurements are input-quality proxies.

Run from the model directory with serving dependencies and FFmpeg:

~~~powershell
python deployment/export_calibration.py --csv TRAIN.csv --videos TRAIN_VIDEOS --model-dir MODEL_DIR --split training --output training.npz
python deployment/export_calibration.py --csv HELD_OUT.csv --videos HELD_OUT_VIDEOS --model-dir MODEL_DIR --split calibration --output calibration.npz
python deployment/build_quality_policy.py --training training.npz --calibration calibration.npz --model-version MODEL_VERSION --output quality-policy.json
~~~

The exporter requires exactly one classified utterance per MELD clip and records content hashes. The builder rejects overlapping splits, requires at least 257 training embeddings, fits held-out temperatures, and derives initial distance/confidence/entropy thresholds. Version, dimensions, temperatures, and finite limits are validated before serving. Evaluate the initial thresholds before release.

Policies support minimum blur, face and transcript-confidence thresholds plus silence/bleep bounds. Failed quality or uncertainty checks return low_confidence or out_of_domain with a reason and no labels. The UI displays this abstention. Facial-only emotion classification still requires integration and independent evaluation.

The OOD evaluation command accepts JSON arrays named in_domain and out_of_domain and computes AUROC with tie handling. No measured non-MELD AUROC is claimed by this change. Actual release weights and independent clips are required to calibrate and validate production settings.

## Tests and operations

Run Python discovery with deployment/ and monitoring/runtime/ on PYTHONPATH. Run npm run test:guardrails and npm run build in the SaaS directory. The tests exercise policy decisions, state paths, errors, account actions, review conflicts, immutable promotion, registry revocation, browser request sequences, and retention cleanup using mocked services.

The stack includes the scan plan, workflow, Data API endpoint, dedicated secret/rotation, recovery DLQ, daily retention, and moderation/OOD alarms. Interrupted workflows route to review or resume a claimed terminal action. Inspect committing jobs and the recovery DLQ before redrive.

See the [runbook](ai-video-sentiment-model-main/monitoring/RUNBOOK.md), [data architecture](ai-video-sentiment-model-main/monitoring/DATA_ARCHITECTURE.md), and [cost model](ai-video-sentiment-model-main/monitoring/COST.md). Live staging requires service permissions, media tooling, database bootstrap, policy fitting, managed scan tests, and restore drills. The current AWS project restricts resources and backups to Sydney.
