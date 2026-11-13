-- Run in the monitoring workgroup. Replace the UTC dates and pseudonymous IDs.
-- The dt/hour partitions represent Firehose delivery time.
SELECT user_id, count(DISTINCT event_id) AS requests,
       approx_percentile(requestlatency, 0.95) AS p95_ms,
       count(DISTINCT IF(errors = 1, event_id, NULL)) AS errors
FROM video_sentiment_monitoring.inference_events
WHERE dt BETWEEN '2026-11-12' AND '2026-11-13'
  AND user_id = '<user-hmac>'
GROUP BY user_id;

SELECT tenant_id, emotion_class, sentiment_class, count(DISTINCT event_id) AS requests
FROM video_sentiment_monitoring.inference_events
WHERE dt = '2026-11-13' AND hour BETWEEN '08' AND '17'
  AND tenant_id = '<tenant-hmac>' AND errors = 0
GROUP BY tenant_id, emotion_class, sentiment_class;
