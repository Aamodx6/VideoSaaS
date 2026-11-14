import unittest
import json
import os
from unittest.mock import patch

from moderation_policy import decision
from telemetry import Observation
from quality import bleep_ratio, classify, distance, precision_matrix, prepare_policy
import numpy as np
from evaluate_ood import auroc, temperature
from safety_store import disposition


class SafetyTests(unittest.TestCase):
    clean = {"media_valid": True, "malware": "NO_THREATS_FOUND", "frame_score": 4, "audio_toxicity": 7}

    def test_moderation_paths(self):
        for changes, result in (
            ({}, ("PASS", None)),
            ({"malware": None}, ("REVIEW", "VALIDATION_UNAVAILABLE")),
            ({"malware": "THREATS_FOUND"}, ("BLOCK", "MALWARE_DETECTED")),
            ({"media_valid": False}, ("BLOCK", "INVALID_MEDIA")),
            ({"frame_score": 65}, ("REVIEW", "VIDEO_REVIEW_REQUIRED")),
            ({"frame_score": 65, "video_score": 5}, ("PASS", None)),
            ({"audio_toxicity": 90}, ("BLOCK", "CONTENT_POLICY")),
            ({"frame_score": 90, "audio_toxicity": None}, ("BLOCK", "CONTENT_POLICY")),
            ({"frame_score": 65, "video_score": 91}, ("BLOCK", "CONTENT_POLICY")),
            ({"audio_toxicity": float("nan")}, ("REVIEW", "MODERATION_UNAVAILABLE")),
        ):
            with self.subTest(changes=changes):
                self.assertEqual(decision({**self.clean, **changes}, 65, 90), result)

    def test_distance_and_abstention(self):
        precision = precision_matrix([[1, 0], [0, 1]])
        self.assertAlmostEqual(distance([3, 4], [0, 0], precision), 5, places=2)
        limits = {"max_distance": 6, "min_confidence": .7, "max_entropy": .9, "min_face_rate": .2}
        high = [[.9, .1], [.8, .2]]
        self.assertEqual(classify({"face_rate": .1}, high, 2, limits), ("out_of_domain", "faces_obscured"))
        self.assertEqual(classify({"face_rate": .8}, high, 7, limits), ("out_of_domain", "embedding_shift"))
        self.assertEqual(classify({"face_rate": .8}, [[.5, .5], [.8, .2]], 2, limits),
                         ("low_confidence", "confidence_below_threshold"))
        self.assertEqual(classify({"face_rate": .8}, high, 2, limits), ("ok", None))

    def test_calibration_and_auroc(self):
        logits = [[10, 0], [10, 0]]
        self.assertGreater(temperature(logits, [0, 1]), 1)
        self.assertEqual(auroc([1, 2], [3, 4]), 1)
        self.assertEqual(auroc([1, 2], [1, 2]), .5)

    def test_emf_flags_and_dimensions(self):
        observation = Observation(sample_rate=0)
        observation.stats.update(ood_detected=True, degraded_input=False)
        event = observation.event(False)
        self.assertEqual((event["OodRate"], event["DegradedInputRate"]), (1, 0))
        self.assertEqual(event["_aws"]["CloudWatchMetrics"][0]["Dimensions"], [["endpoint", "model_version"]])

    def test_strike_thresholds(self):
        self.assertEqual([disposition(n) for n in (0, 1, 2, 3, 4, 5)],
                         ["active", "warn", "warn", "throttle", "throttle", "suspend"])
        with self.assertRaises(ValueError):
            disposition(1, 5, 3)

    def test_tone_proxy_and_silence(self):
        self.assertEqual(bleep_ratio(np.zeros(16000)), 0)
        tone = .2 * np.sin(2 * np.pi * 1000 * np.arange(16000) / 16000)
        self.assertGreater(bleep_ratio(tone), .9)

    def test_policy_rejects_wrong_version_and_nonfinite_limits(self):
        policy = {"model_version": "v1", "temperature": {"emotion": 1, "sentiment": 1},
                  "embedding": {"mean": [0] * 256, "covariance": np.eye(256).tolist()},
                  "limits": {"max_distance": 5, "min_confidence": .4, "max_entropy": float("nan")}}
        with self.assertRaisesRegex(ValueError, "version"):
            prepare_policy(policy, "v2")
        with self.assertRaisesRegex(ValueError, "thresholds"):
            prepare_policy(policy, "v1")


class ModerationContractTests(unittest.TestCase):
    def setUp(self):
        self.settings = patch.dict(os.environ, {
            "AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test",
            "AWS_DEFAULT_REGION": "ap-southeast-2", "JOBS": "jobs", "VIDEOS": "processing",
            "QUARANTINE": "quarantine",
        })
        self.settings.start()
        self.addCleanup(self.settings.stop)
        import data_jobs
        import moderate
        self.api = data_jobs
        self.worker = moderate
        self.policy = patch("safety_store.account_policy", return_value="active").start()
        patch("botocore.endpoint.Endpoint.make_request", side_effect=AssertionError("Unexpected live AWS call")).start()
        self.addCleanup(patch.stopall)

    def test_blocked_poll_returns_stable_code(self):
        event = {"httpMethod": "GET", "headers": {"X-Aspect-Api-Key": "a" * 32},
                 "pathParameters": {"jobId": "job"}}
        with patch.object(self.api, "fetch", side_effect=[
            {"tenant_id": "tenant", "user_id": "user", "expires_at": 9999999999},
            {"status": "BLOCKED", "moderation_code": "CONTENT_POLICY", "user_id": "user",
             "expires_at": 9999999999},
        ]), patch.object(self.api, "rate_allowed", return_value=True):
            response = self.api.api(event, None)
        self.assertEqual(response["statusCode"], 422)
        self.assertEqual(json.loads(response["body"])["code"], "CONTENT_POLICY")

    def test_only_pass_copies_exact_version(self):
        job = {"status": "PENDING_MODERATION", "video_key": "uploads/test.mp4", "upload_version": "one",
               "user_id": "user"}
        cases = [(SafetyTests.clean, True), ({"malware": "THREATS_FOUND"}, False), ({}, False)]
        for report, promoted in cases:
            with (patch.object(self.worker, "fetch", return_value=job),
                  patch.object(self.worker.s3, "copy_object") as copy,
                  patch.object(self.worker.s3, "head_object", return_value={"Metadata": {}}),
                  patch.object(self.worker.ddb, "update_item") as update,
                  patch.object(self.worker, "audit"),
                  patch.object(self.worker, "cleanup"),
                  patch("builtins.print")):
                self.worker.finish({"tenant_id": "tenant", "job_id": "job", "assessment": report}, None)
                self.assertEqual(update.call_count, 2)
                self.assertEqual(copy.called, promoted)
                if promoted:
                    self.assertEqual(copy.call_args.kwargs["CopySource"]["VersionId"], "one")

    def test_worker_never_submits_unapproved_job(self):
        event = {"Records": [{"body": '{"tenant_id":"tenant","job_id":"job"}'}]}
        with patch.object(self.api, "fetch", return_value={"status": "QUEUED"}), patch.object(self.api.boto3, "client") as client:
            with self.assertRaisesRegex(RuntimeError, "no moderation approval"):
                self.api.submit(event, None)
            client.return_value.invoke_endpoint_async.assert_not_called()

    def test_suspended_account_rejected_before_upload(self):
        self.policy.return_value = "suspend"
        with patch.object(self.api, "fetch", return_value={
            "tenant_id": "tenant", "user_id": "user", "expires_at": 9999999999,
        }), patch.object(self.api.s3, "generate_presigned_post") as sign:
            response = self.api.api({"headers": {"X-Aspect-Api-Key": "a" * 32},
                                     "httpMethod": "POST", "resource": "/uploads"}, None)
            self.assertEqual(response["statusCode"], 403)
            self.assertEqual(json.loads(response["body"])["code"], "ACCOUNT_SUSPENDED")
            sign.assert_not_called()

    def test_aurora_outage_prevents_upload(self):
        self.policy.side_effect = RuntimeError("unavailable")
        with patch.object(self.api, "fetch", return_value={
            "tenant_id": "tenant", "user_id": "user", "expires_at": 9999999999,
        }):
            response = self.api.api({"headers": {"X-Aspect-Api-Key": "a" * 32}, "httpMethod": "POST"}, None)
            self.assertEqual(response["statusCode"], 503)
            self.assertEqual(json.loads(response["body"])["code"], "SAFETY_CHECK_UNAVAILABLE")

    def test_quota_reads_dynamodb_without_an_aurora_check(self):
        with patch.object(self.api, "fetch", side_effect=[
            {"tenant_id": "t", "user_id": "u", "monthly_limit": 100, "expires_at": 9999999999},
            {"used": 7},
        ]), patch.object(self.api, "rate_allowed", return_value=True):
            response = self.api.api({"headers": {"X-Aspect-Api-Key": "a" * 32},
                                     "httpMethod": "GET", "resource": "/quota"}, None)
            self.assertEqual(response["statusCode"], 200)
            self.assertEqual(json.loads(response["body"])["requests_used"], 7)
            self.policy.assert_not_called()

    def test_cannot_approve_unscanned_review(self):
        job = {"status": "REVIEW", "moderation_report": "{}", "user_id": "user"}
        with patch.object(self.worker, "fetch", return_value=job), patch.object(self.worker, "copy_once") as copy:
            response = self.worker.review({"pathParameters": {"tenant": "tenant", "jobId": "job"},
                                          "httpMethod": "POST", "body": '{"decision":"PASS"}'}, None)
            self.assertEqual(response["statusCode"], 409)
            copy.assert_not_called()

    def test_review_approval_requires_completed_scans(self):
        report = {**SafetyTests.clean, "audio_toxicity": 70}
        job = {"status": "REVIEW", "moderation_report": json.dumps(report), "user_id": "user"}
        job.update(video_key="upload", upload_version="version")
        with (patch.object(self.worker, "fetch", return_value=job),
              patch.object(self.worker.s3, "head_object", return_value={}),
              patch.object(self.worker, "commit_decision") as commit,
              patch("builtins.print")):
            response = self.worker.review({"pathParameters": {"tenant": "tenant", "jobId": "job"},
                                          "httpMethod": "POST", "body": '{"decision":"PASS"}'}, None)
            self.assertEqual(response["statusCode"], 200)
            self.assertEqual(commit.call_args.args[2], "PASS")

    def test_conflicting_reviewer_cannot_commit(self):
        with patch.object(self.worker.ddb, "update_item", side_effect=self.api.ddb.exceptions.ConditionalCheckFailedException(
                {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")), patch.object(self.worker, "audit") as audit:
            self.assertFalse(self.worker.commit_decision(
                {"tenant_id": "t", "job_id": "j"}, {}, "PASS", "HUMAN_REVIEW_PASS", {}, "reviewer"))
            audit.assert_not_called()

    def test_wrong_language_never_runs_toxicity_as_english(self):
        with patch.object(self.worker.boto3, "client") as client:
            client.return_value.get_transcription_job.return_value = {"TranscriptionJob": {
                "TranscriptionJobStatus": "COMPLETED", "LanguageCode": "fr-FR", "IdentifiedLanguageScore": .99}}
            self.assertEqual(self.worker.poll_audio({}, {"language": "lang"}), {})
            client.return_value.start_transcription_job.assert_not_called()

    def test_retention_cleanup_deletes_versions_and_markers(self):
        with patch.object(self.worker.s3, "get_paginator") as pager, patch.object(self.worker.s3, "delete_objects") as delete:
            pager.return_value.paginate.return_value = [{"Versions": [
                {"Key": "a", "VersionId": "v1"}, {"Key": "another", "VersionId": "v2"}],
                "DeleteMarkers": [{"Key": "a", "VersionId": "v3"}]}]
            delete.return_value = {}
            self.worker.delete_versions("bucket", "a")
            self.assertEqual(delete.call_args.kwargs["Delete"]["Objects"], [
                {"Key": "a", "VersionId": "v1"}, {"Key": "a", "VersionId": "v3"}])

    def test_erasure_waits_for_managed_jobs_to_finish(self):
        with patch.object(self.worker.boto3, "client") as client, patch.object(self.worker, "delete_versions") as delete:
            client.return_value.get_transcription_job.return_value = {"TranscriptionJob": {
                "TranscriptionJobStatus": "IN_PROGRESS"}}
            with self.assertRaisesRegex(RuntimeError, "must finish"):
                self.worker.cleanup({"user_id": "u"}, {"job_id": "j", "tenant_id": "t"}, "BLOCK")
            delete.assert_not_called()
