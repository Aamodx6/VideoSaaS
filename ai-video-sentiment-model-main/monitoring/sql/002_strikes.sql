BEGIN;

CREATE ROLE aspect_safety LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
GRANT USAGE ON SCHEMA aspect TO aspect_safety;

-- Opaque subject IDs bridge the async identity store and the hosted app.
-- Moderation subjects need not have a provisioned Aurora membership row.
CREATE TABLE aspect.strikes (
    tenant_id text NOT NULL,
    user_id text NOT NULL,
    job_id text NOT NULL,
    reason_code text NOT NULL CHECK (reason_code IN ('CONTENT_POLICY','MALWARE_DETECTED','HUMAN_REVIEW_BLOCK')),
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, user_id, job_id)
);
CREATE INDEX strikes_subject ON aspect.strikes (tenant_id, user_id, expires_at);

CREATE TABLE aspect.moderation_events (
    tenant_id text NOT NULL,
    user_id text NOT NULL,
    job_id text NOT NULL,
    source text NOT NULL CHECK (source IN ('automated','reviewer')),
    decision text NOT NULL CHECK (decision IN ('PASS','REVIEW','BLOCK')),
    reason_code text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, user_id, job_id, source)
);
CREATE INDEX moderation_events_expiry ON aspect.moderation_events (expires_at);

ALTER TABLE aspect.strikes ENABLE ROW LEVEL SECURITY;
ALTER TABLE aspect.strikes FORCE ROW LEVEL SECURITY;
ALTER TABLE aspect.moderation_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE aspect.moderation_events FORCE ROW LEVEL SECURITY;

-- End-user role access is scoped to both the active tenant and subject.
CREATE POLICY strikes_self ON aspect.strikes FOR SELECT TO aspect_app
    USING (tenant_id = current_setting('aspect.tenant_id', true)
           AND user_id = current_setting('aspect.user_id', true));
GRANT SELECT ON aspect.strikes TO aspect_app;
GRANT SELECT, INSERT, DELETE ON aspect.strikes, aspect.moderation_events TO aspect_safety;
CREATE POLICY safety_strikes ON aspect.strikes TO aspect_safety USING (true) WITH CHECK (true);
CREATE POLICY safety_events ON aspect.moderation_events TO aspect_safety USING (true) WITH CHECK (true);

-- Deploy with the operator secret used by the RDS Data API safety worker.
DO $$
DECLARE table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY['strikes','moderation_events'] LOOP
        EXECUTE format('CREATE POLICY safety_operator ON aspect.%I TO %I USING (true) WITH CHECK (true)',
                       table_name, current_user);
    END LOOP;
END $$;
COMMIT;
