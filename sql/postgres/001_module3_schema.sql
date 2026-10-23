-- =============================================================================
-- OmniSync - Module 3 PostgreSQL schema (OLTP side)
-- Identity Stitching, Dynamic Return Friction & Reverse-ETL landing tables.
--
-- Apply with:  python -m scripts.apply_schemas --target postgres --localhost
-- Safe to re-run: every statement is idempotent (IF NOT EXISTS).
--
-- Three groups of tables:
--   1. Source tables   - transactional records the synthetic generator writes.
--   2. Serving tables  - read by FastAPI at checkout / return time (0ms added latency
--                        because the values are pre-computed, never calculated on request).
--   3. Stage tables    - landing zone for Exasol `EXPORT ... INTO JDBC`. EXPORT can only
--                        INSERT, so Celery loads the stage table and then upserts it into
--                        the serving table inside one Postgres transaction.
--
-- NOT defined here (owned by other modules): `inventory` (OCC ledger, CHECK stock >= 0).
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. Source tables
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS users (
    user_id             TEXT PRIMARY KEY,               -- e.g. 'user_000123'
    shopify_customer_id TEXT UNIQUE,                    -- Shopify account id (NULL for guests)
    email               TEXT,
    full_name           TEXT,
    phone               TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_users_email ON users (lower(email));

CREATE TABLE IF NOT EXISTS purchases (
    purchase_id  TEXT PRIMARY KEY,                      -- e.g. 'PUR-00000001'
    user_id      TEXT NOT NULL,                         -- no FK: guest checkouts may use a device id
    item_id      TEXT NOT NULL,                         -- matches inventory.item_id (e.g. 'SKU-AIR-JORDAN')
    quantity     INTEGER NOT NULL CHECK (quantity > 0),
    unit_price   NUMERIC(10, 2) NOT NULL CHECK (unit_price >= 0),
    promo_code   TEXT,                                  -- weak identity signal, never a deterministic match
    purchased_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_purchases_user_time ON purchases (user_id, purchased_at);

CREATE TABLE IF NOT EXISTS returns (
    return_id     TEXT PRIMARY KEY,                     -- e.g. 'RET-00000001'
    purchase_id   TEXT NOT NULL REFERENCES purchases (purchase_id),
    user_id       TEXT NOT NULL,
    reason        TEXT,
    refund_amount NUMERIC(10, 2) NOT NULL CHECK (refund_amount >= 0),
    returned_at   TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_returns_user_time ON returns (user_id, returned_at);

CREATE TABLE IF NOT EXISTS support_tickets (
    ticket_id       TEXT PRIMARY KEY,                   -- Zendesk id, e.g. 'ZD-100001'
    user_id         TEXT,                               -- NULL when the requester is not logged in
    requester_email TEXT,
    subject         TEXT NOT NULL,
    description     TEXT NOT NULL,
    priority        TEXT NOT NULL DEFAULT 'normal',
    created_at      TIMESTAMPTZ NOT NULL
);

-- -----------------------------------------------------------------------------
-- 2. Serving tables (written ONLY by the Celery reverse-ETL upsert)
-- -----------------------------------------------------------------------------

-- Golden Record summary. One row per resolved real-world customer.
CREATE TABLE IF NOT EXISTS golden_customers (
    golden_customer_id TEXT PRIMARY KEY,                -- e.g. 'CUST-GOLDEN-000042'
    primary_email      TEXT,
    member_count       INTEGER NOT NULL CHECK (member_count >= 1),
    total_spend_usd    NUMERIC(12, 2) NOT NULL DEFAULT 0,
    first_seen_at      TIMESTAMPTZ,
    last_seen_at       TIMESTAMPTZ,
    synced_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Pre-computed Return Risk Level, keyed by every id a checkout request might carry
-- (Shopify user id, guest device id, ...). FastAPI does a primary-key lookup only.
CREATE TABLE IF NOT EXISTS user_risk_cache (
    user_id            TEXT PRIMARY KEY,
    golden_customer_id TEXT,
    risk_level         SMALLINT NOT NULL CHECK (risk_level BETWEEN 1 AND 5),
    return_rate        NUMERIC(5, 4) NOT NULL CHECK (return_rate >= 0),  -- returns / purchases, 6-month window
    purchases_6m       INTEGER NOT NULL CHECK (purchases_6m >= 0),
    returns_6m         INTEGER NOT NULL CHECK (returns_6m >= 0),
    computed_at        TIMESTAMPTZ NOT NULL DEFAULT now(), -- when Exasol calculated it
    source_watermark   TIMESTAMPTZ,                        -- upstream source watermark timestamp
    synced_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_user_risk_cache_golden ON user_risk_cache (golden_customer_id);

-- Members of each golden customer (the stitched Shopify accounts, guest devices and Zendesk requesters), with
-- the match confidence. Replaced as a whole on every sync. Read by the Golden Record dashboard pages.
CREATE TABLE IF NOT EXISTS golden_customer_members (
    golden_customer_id TEXT NOT NULL,
    node_id            TEXT NOT NULL,                   -- 'SHOPIFY:user_000123' | 'DEVICE:dev_9f2c' | 'ZENDESK:a@b.com'
    node_type          TEXT NOT NULL,                   -- SHOPIFY | DEVICE | ZENDESK
    source_id          TEXT NOT NULL,                   -- the id as the source system knows it
    confidence         NUMERIC(5, 4) NOT NULL,
    synced_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (golden_customer_id, node_id)
);

CREATE INDEX IF NOT EXISTS idx_golden_members_source ON golden_customer_members (source_id);
CREATE INDEX IF NOT EXISTS idx_golden_customers_email ON golden_customers (lower(primary_email));

-- Identity evidence tracking events contributing to golden identity resolution
CREATE TABLE IF NOT EXISTS identity_evidence (
    source_id           TEXT NOT NULL,
    event_type          TEXT NOT NULL,
    confidence_weight   NUMERIC(5, 4) NOT NULL,
    linked_to_golden_id TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_identity_evidence_source ON identity_evidence (source_id);
CREATE INDEX IF NOT EXISTS idx_identity_evidence_golden ON identity_evidence (linked_to_golden_id);

-- Candidate pairwise links awaiting probabilistic merge or human operator review
CREATE TABLE IF NOT EXISTS candidate_links (
    node_a              TEXT NOT NULL,
    node_b              TEXT NOT NULL,
    model_score         NUMERIC(5, 4) NOT NULL,
    human_review_status TEXT NOT NULL DEFAULT 'PENDING',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_candidate_links_nodes ON candidate_links (node_a, node_b);
CREATE INDEX IF NOT EXISTS idx_candidate_links_status ON candidate_links (human_review_status);

-- -----------------------------------------------------------------------------
-- 3. Stage tables (target of Exasol EXPORT ... INTO JDBC; truncated every sync)
--    No constraints on purpose: EXPORT must never fail half-way on a bad row.
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS golden_customers_stage (
    golden_customer_id TEXT,
    primary_email      TEXT,
    member_count       INTEGER,
    total_spend_usd    NUMERIC(12, 2),
    first_seen_at      TIMESTAMPTZ,
    last_seen_at       TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS user_risk_cache_stage (
    user_id            TEXT,
    golden_customer_id TEXT,
    risk_level         SMALLINT,
    return_rate        NUMERIC(5, 4),
    purchases_6m       INTEGER,
    returns_6m         INTEGER,
    computed_at        TIMESTAMPTZ,
    source_watermark   TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS golden_customer_members_stage (
    golden_customer_id TEXT,
    node_id            TEXT,
    node_type          TEXT,
    source_id          TEXT,
    confidence         NUMERIC(5, 4)
);
