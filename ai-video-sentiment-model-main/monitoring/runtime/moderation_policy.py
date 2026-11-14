"""Decision logic for trusted moderation results. Missing scans never pass."""

import math


def decision(report, review, block):
    if not 0 <= review < block <= 100:
        raise ValueError("Invalid moderation thresholds")
    if report.get("media_valid") is False:
        return "BLOCK", "INVALID_MEDIA"
    if report.get("malware") == "THREATS_FOUND":
        return "BLOCK", "MALWARE_DETECTED"
    if report.get("media_valid") is not True or report.get("malware") != "NO_THREATS_FOUND":
        return "REVIEW", "VALIDATION_UNAVAILABLE"

    def valid(value):
        return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 100

    frames = report.get("frame_score")
    audio = report.get("audio_toxicity")
    if (valid(frames) and frames >= block) or (valid(audio) and audio >= block):
        return "BLOCK", "CONTENT_POLICY"
    if not valid(frames) or not valid(audio):
        return "REVIEW", "MODERATION_UNAVAILABLE"
    if frames >= review:
        full = report.get("video_score")
        if not valid(full):
            return "REVIEW", "VIDEO_REVIEW_REQUIRED"
        if full >= block:
            return "BLOCK", "CONTENT_POLICY"
        if full >= review:
            return "REVIEW", "CONTENT_REVIEW_REQUIRED"
    if audio >= review:
        return "REVIEW", "CONTENT_REVIEW_REQUIRED"
    return "PASS", None
