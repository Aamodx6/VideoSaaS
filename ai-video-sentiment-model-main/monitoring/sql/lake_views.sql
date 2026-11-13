-- Run in the operations workgroup after selecting the lake Glue database.
-- Supply a tenant equality and a date range when querying any view.
CREATE OR REPLACE VIEW usage_per_tenant AS
SELECT dt, tenant_id, sum(requests) AS requests, sum(succeeded) AS succeeded
FROM usage GROUP BY dt, tenant_id;

CREATE OR REPLACE VIEW cost_per_request AS
SELECT dt, tenant_id, sum(requests) AS requests,
       sum(priced_requests) AS priced_requests, sum(cost_usd) AS allocated_cost_usd,
       sum(cost_usd) / nullif(sum(priced_requests), 0) AS avg_priced_request_usd
FROM usage GROUP BY dt, tenant_id;

CREATE OR REPLACE VIEW accuracy_per_tenant AS
SELECT dt, tenant_id, sum(labeled) AS labeled, sum(correct) AS correct,
       CAST(sum(correct) AS double) / nullif(sum(labeled), 0) AS accuracy
FROM accuracy GROUP BY dt, tenant_id;

CREATE OR REPLACE VIEW drift_history AS
SELECT dt, tenant_id, model_version, drift_score, confidence FROM drift;
