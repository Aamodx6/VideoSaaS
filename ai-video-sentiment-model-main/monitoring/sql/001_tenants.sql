BEGIN;

CREATE ROLE aspect_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
CREATE SCHEMA aspect;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA aspect TO aspect_app;

CREATE TABLE aspect.users (
    id uuid PRIMARY KEY,
    email text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX users_email_unique ON aspect.users (lower(email));

CREATE TABLE aspect.organizations (
    id uuid PRIMARY KEY,
    name text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE aspect.memberships (
    tenant_id uuid NOT NULL REFERENCES aspect.organizations(id) ON DELETE CASCADE,
    user_id uuid NOT NULL REFERENCES aspect.users(id) ON DELETE CASCADE,
    role text NOT NULL CHECK (role IN ('owner', 'admin', 'member')),
    PRIMARY KEY (tenant_id, user_id)
);
CREATE INDEX memberships_user_idx ON aspect.memberships (user_id, tenant_id);

CREATE TABLE aspect.plans (
    id text PRIMARY KEY,
    monthly_requests bigint NOT NULL CHECK (monthly_requests > 0),
    monthly_price_minor bigint NOT NULL CHECK (monthly_price_minor >= 0),
    currency char(3) NOT NULL
);
CREATE TABLE aspect.subscriptions (
    tenant_id uuid PRIMARY KEY REFERENCES aspect.organizations(id),
    plan_id text NOT NULL REFERENCES aspect.plans(id),
    status text NOT NULL CHECK (status IN ('active', 'paused', 'canceled')),
    period_end timestamptz NOT NULL
);
CREATE TABLE aspect.invoices (
    tenant_id uuid NOT NULL REFERENCES aspect.organizations(id),
    id uuid NOT NULL,
    issued_at timestamptz NOT NULL DEFAULT now(),
    amount_minor bigint NOT NULL CHECK (amount_minor >= 0),
    currency char(3) NOT NULL,
    PRIMARY KEY (tenant_id, id)
);
CREATE TABLE aspect.audit_log (
    tenant_id uuid NOT NULL REFERENCES aspect.organizations(id),
    id bigint GENERATED ALWAYS AS IDENTITY,
    actor_id uuid REFERENCES aspect.users(id) ON DELETE SET NULL,
    action text NOT NULL CHECK (action ~ '^[a-z_]{1,64}$'),
    occurred_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id)
);
CREATE INDEX audit_log_expiry_idx ON aspect.audit_log (occurred_at);

-- Trusted server connections set these variables transaction-locally after
-- authentication. Use the admin connection to bootstrap organizations and memberships.
CREATE FUNCTION aspect.active_user() RETURNS uuid
LANGUAGE sql STABLE AS $$ SELECT nullif(current_setting('aspect.user_id', true), '')::uuid $$;
CREATE FUNCTION aspect.active_tenant() RETURNS uuid
LANGUAGE sql STABLE AS $$ SELECT nullif(current_setting('aspect.tenant_id', true), '')::uuid $$;
CREATE FUNCTION aspect.is_member(candidate uuid) RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT candidate = aspect.active_tenant()
       AND EXISTS (
           SELECT 1 FROM aspect.memberships m
           WHERE m.tenant_id = candidate AND m.user_id = aspect.active_user()
       )
$$;

ALTER TABLE aspect.users ENABLE ROW LEVEL SECURITY;
ALTER TABLE aspect.users FORCE ROW LEVEL SECURITY;
CREATE POLICY users_self ON aspect.users FOR SELECT TO aspect_app USING (id = aspect.active_user());
ALTER TABLE aspect.memberships ENABLE ROW LEVEL SECURITY;
ALTER TABLE aspect.memberships FORCE ROW LEVEL SECURITY;
CREATE POLICY memberships_self ON aspect.memberships FOR SELECT TO aspect_app
    USING (tenant_id = aspect.active_tenant() AND user_id = aspect.active_user());
ALTER TABLE aspect.organizations ENABLE ROW LEVEL SECURITY;
ALTER TABLE aspect.organizations FORCE ROW LEVEL SECURITY;
CREATE POLICY organizations_member ON aspect.organizations FOR SELECT TO aspect_app
    USING (aspect.is_member(id));

DO $$
DECLARE table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY['subscriptions', 'invoices', 'audit_log'] LOOP
        EXECUTE format('ALTER TABLE aspect.%I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE aspect.%I FORCE ROW LEVEL SECURITY', table_name);
        EXECUTE format('CREATE POLICY tenant_read ON aspect.%I FOR SELECT TO aspect_app USING (aspect.is_member(tenant_id))',
                       table_name);
    END LOOP;
END $$;
GRANT SELECT ON ALL TABLES IN SCHEMA aspect TO aspect_app;

-- The migration owner is deliberately separate from aspect_app. This policy
-- permits the deployment operator to seed data and process verified erasures.
DO $$
DECLARE table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY['users', 'organizations', 'memberships', 'subscriptions', 'invoices', 'audit_log'] LOOP
        EXECUTE format('CREATE POLICY operator ON aspect.%I TO %I USING (true) WITH CHECK (true)',
                       table_name, current_user);
    END LOOP;
END $$;
COMMIT;
