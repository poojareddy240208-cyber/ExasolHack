-- =============================================================================
-- OmniSync - Module 3 Exasol schema (OLAP side, "the brain")
-- Identity Stitching (Golden Record) and Serial-Returner scoring.
--
-- Apply with:  python -m scripts.apply_schemas --target exasol --localhost
-- Safe to re-run: every statement is idempotent (IF NOT EXISTS).
-- Exasol is passive: nothing here schedules or pushes data. Celery Beat triggers
-- every computation and every EXPORT ... INTO JDBC.
--
-- Note: Exasol has no JSON column type. Raw web events keep the unstructured JSON
-- in a VARCHAR column and are parsed in SQL with JSON_VALUE (Exasol 7.1+).
--
-- Layers:
--   RAW_*              copies of source data + unstructured web events
--   IDENTITY_*         intermediate stitching work (nodes, scored pairs)
--   GOLDEN_*, USER_*   final outputs exported to PostgreSQL by Celery Beat
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS OMNISYNC_ANALYTICS;
OPEN SCHEMA OMNISYNC_ANALYTICS;

-- -----------------------------------------------------------------------------
-- RAW layer
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS RAW_USERS (
    USER_ID             VARCHAR(64) NOT NULL,
    SHOPIFY_CUSTOMER_ID VARCHAR(64),
    EMAIL               VARCHAR(255),
    FULL_NAME           VARCHAR(255),
    PHONE               VARCHAR(64),
    CREATED_AT          TIMESTAMP
);

CREATE TABLE IF NOT EXISTS RAW_PURCHASES (
    PURCHASE_ID  VARCHAR(64) NOT NULL,
    USER_ID      VARCHAR(64) NOT NULL,
    ITEM_ID      VARCHAR(64) NOT NULL,
    QUANTITY     DECIMAL(9, 0) NOT NULL,
    UNIT_PRICE   DECIMAL(10, 2) NOT NULL,
    PROMO_CODE   VARCHAR(64),
    PURCHASED_AT TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS RAW_RETURNS (
    RETURN_ID     VARCHAR(64) NOT NULL,
    PURCHASE_ID   VARCHAR(64) NOT NULL,
    USER_ID       VARCHAR(64) NOT NULL,
    REASON        VARCHAR(255),
    REFUND_AMOUNT DECIMAL(10, 2) NOT NULL,
    RETURNED_AT   TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS RAW_SUPPORT_TICKETS (
    TICKET_ID       VARCHAR(64) NOT NULL,
    USER_ID         VARCHAR(64),
    REQUESTER_EMAIL VARCHAR(255),
    SUBJECT         VARCHAR(500) NOT NULL,
    DESCRIPTION     VARCHAR(20000) NOT NULL,
    PRIORITY        VARCHAR(16),
    CREATED_AT      TIMESTAMP NOT NULL
);

-- Fragmented clickstream. PAYLOAD is the unstructured JSON exactly as the browser sent it,
-- e.g. {"page": "/p/air-jordan", "email_hint": "a@b.com", "promo": "SAVE10", "cart_value": 120.5}
CREATE TABLE IF NOT EXISTS RAW_WEB_EVENTS (
    EVENT_ID   DECIMAL(18, 0) IDENTITY,
    EVENT_TS   TIMESTAMP NOT NULL,
    DEVICE_ID  VARCHAR(64) NOT NULL,       -- anonymous cookie / device fingerprint
    SESSION_ID VARCHAR(64),
    USER_ID    VARCHAR(64),                -- only set once the visitor logs in
    PAYLOAD    VARCHAR(20000) NOT NULL
);

-- -----------------------------------------------------------------------------
-- IDENTITY layer (Golden Record work tables, rebuilt by every stitching run)
-- -----------------------------------------------------------------------------

-- One row per identity fragment: a Shopify user, a Zendesk requester or a guest device.
-- EMAIL is stored normalised (lower-case, "+tag" removed) so aliases compare equal.
CREATE TABLE IF NOT EXISTS IDENTITY_NODES (
    NODE_ID    VARCHAR(100) NOT NULL,      -- 'SHOPIFY:user_000123' | 'ZENDESK:a@b.com' | 'DEVICE:dev_9f2c'
    NODE_TYPE  VARCHAR(16) NOT NULL,       -- SHOPIFY | ZENDESK | DEVICE
    SOURCE_ID  VARCHAR(255) NOT NULL,      -- id in the source system
    EMAIL      VARCHAR(255),
    PHONE      VARCHAR(64),
    FULL_NAME  VARCHAR(255),
    FIRST_SEEN TIMESTAMP,
    LAST_SEEN  TIMESTAMP
);

-- Promo codes each node has used. Coupon-site codes (too many holders) are removed during
-- stitching: they carry no identity information.
CREATE TABLE IF NOT EXISTS IDENTITY_NODE_PROMOS (
    NODE_ID    VARCHAR(100) NOT NULL,
    PROMO_CODE VARCHAR(64) NOT NULL,
    USES       DECIMAL(9, 0) NOT NULL
);

-- Pairs that share an account link: a device that logged in, or a ticket raised while logged in.
CREATE TABLE IF NOT EXISTS IDENTITY_LINKS (
    NODE_A      VARCHAR(100) NOT NULL,     -- always NODE_A < NODE_B
    NODE_B      VARCHAR(100) NOT NULL,
    LINK_SOURCE VARCHAR(32) NOT NULL       -- WEB_LOGIN | TICKET_USER
);

-- Pairs that share a (rare) promo code, with the evidence weight that code is worth.
CREATE TABLE IF NOT EXISTS IDENTITY_PROMO_PAIRS (
    NODE_A       VARCHAR(100) NOT NULL,
    NODE_B       VARCHAR(100) NOT NULL,
    PROMO_WEIGHT DECIMAL(6, 4) NOT NULL
);

-- Every candidate pair with its evidence and probability. Scoring is a log-odds sum
-- (Fellegi-Sunter style): LOGIT = BIAS + w_email*EMAIL_MATCH + w_link*ACCOUNT_LINK + PROMO_WEIGHT,
-- MATCH_SCORE = 1 / (1 + EXP(-LOGIT)). The promo weight is capped below |BIAS|, so a shared
-- promo code can never push a pair over the merge threshold on its own.
CREATE TABLE IF NOT EXISTS IDENTITY_PAIR_SCORES (
    NODE_A       VARCHAR(100) NOT NULL,
    NODE_B       VARCHAR(100) NOT NULL,
    EMAIL_MATCH  DECIMAL(1, 0) NOT NULL,
    ACCOUNT_LINK DECIMAL(1, 0) NOT NULL,
    PROMO_WEIGHT DECIMAL(6, 4) NOT NULL,
    LOGIT        DECIMAL(8, 4) NOT NULL,
    MATCH_SCORE  DECIMAL(5, 4) NOT NULL,   -- probability the two nodes are the same person
    IS_MERGED    DECIMAL(1, 0) NOT NULL,   -- 1 when MATCH_SCORE >= merge threshold
    SCORED_AT    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Connected-component labels: nodes with the same label belong to one golden customer.
CREATE TABLE IF NOT EXISTS IDENTITY_LABELS (
    NODE_ID       VARCHAR(100) NOT NULL,
    CLUSTER_LABEL VARCHAR(100) NOT NULL
);

-- -----------------------------------------------------------------------------
-- OUTPUT layer (exported to PostgreSQL by Celery Beat)
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS GOLDEN_CUSTOMERS (
    GOLDEN_CUSTOMER_ID VARCHAR(64) NOT NULL,   -- 'CUST-GOLDEN-000042'
    PRIMARY_EMAIL      VARCHAR(255),
    MEMBER_COUNT       DECIMAL(9, 0) NOT NULL,
    TOTAL_SPEND_USD    DECIMAL(12, 2) NOT NULL,
    FIRST_SEEN_AT      TIMESTAMP,
    LAST_SEEN_AT       TIMESTAMP
);

-- Which identity fragments belong to which golden customer, with confidence.
CREATE TABLE IF NOT EXISTS GOLDEN_CUSTOMER_MEMBERS (
    GOLDEN_CUSTOMER_ID VARCHAR(64) NOT NULL,
    NODE_ID            VARCHAR(100) NOT NULL,
    CONFIDENCE         DECIMAL(5, 4) NOT NULL
);

-- Serial-returner result, one row per id a checkout request might carry.
-- Risk bands (return rate = returns / purchases over the last 6 months):
--   1: < 10%   2: 10% - <25%   3: 25% - <40%   4: 40% - 60%   5: > 60%
-- (Levels 1, 3, 5 come from the PRD; levels 2 and 4 fill the gaps.)
CREATE TABLE IF NOT EXISTS USER_RETURN_RISK (
    USER_ID            VARCHAR(64) NOT NULL,
    GOLDEN_CUSTOMER_ID VARCHAR(64),
    RISK_LEVEL         DECIMAL(1, 0) NOT NULL,
    RETURN_RATE        DECIMAL(5, 4) NOT NULL,
    PURCHASES_6M       DECIMAL(9, 0) NOT NULL,
    RETURNS_6M         DECIMAL(9, 0) NOT NULL,
    COMPUTED_AT        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
