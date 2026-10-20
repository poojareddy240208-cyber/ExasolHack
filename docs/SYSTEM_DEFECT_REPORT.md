# OmniSync - System Defect & Vulnerability Report

> **Status update (2026-10-20):** the fixes from the follow-up sign-off audit are applied on `Vyom_Changes`:
> C1 requirements (already on main), C2 bootstrap script + compose service, C3 webhook fast-fail (79 s -> a few
> seconds, measured), C4 scheduled VIP churn radar with campaign de-duplication, C5 UDF now `RETURNS DOUBLE`
> and the join rewritten, C6 inventory telemetry + buffer query. Still open: H1 real model weights, H2 Exasol
> script-language container, H3 webhook signature, H6 OCC test against real Postgres, H7 COD enforcement.
> Nothing here has been run against live Exasol/Postgres/Redis containers.

**Audited:** `origin/main` at `d84de43` (exported to a scratch folder, nothing on any branch was changed)
**Date:** 2026-10-19

**How each finding was established** (so you can judge how much to trust it):
- **[RAN]** I executed it and saw the result.
- **[READ]** Established by reading the code; not executed.
- **[UNVERIFIED]** Needs a real Docker/Exasol run to confirm. Docker's engine was not reachable during the audit, so nothing that needs live containers could be checked.

**Verdict: this is NOT "zero critical issues". 6 critical, 8 high.** The code that exists is mostly sound and well tested in isolation; the critical problems are in the glue: a corrupted dependency file, nothing that bootstraps a fresh stack, an endpoint that hangs and loses data when Redis is down, and two pipelines (Module 2 sync, Module 4 retention) that are stubbed or never invoked.

---

## Step 1 - Environment & test results

| Check | Result |
|---|---|
| `pytest -v` on a clean machine | **3 test files fail to collect** (`lightgbm` and `psycopg` missing) - [RAN] |
| `pytest -v` with those two packages installed | **71 passed, 0 failed** - [RAN] |
| `black --check` (CI command) | **Fails**: `backend/models/sales.py` would be reformatted - [RAN] |
| `flake8` blocking class (E9,F63,F7,F82) | 0 errors. 18 non-blocking warnings (unused imports, trailing whitespace, one C901 complexity 13) - [RAN] |
| Test warnings | pytest-asyncio `asyncio_default_fixture_loop_scope` unset; Starlette `httpx` deprecation; **unclosed sqlite connections** (ResourceWarning) from tests that never dispose their engine - [RAN] |
| `docker compose config` | Valid apart from the harmless obsolete `version:` warning; requires a `.env` file - [RAN] |
| Services communicate | **Not verified live.** Statically: all 6 services share `omnisync_network`, hostnames (`postgres`, `redis`, `exasol`) match `config.py` defaults, ports map correctly, Celery uses Redis db 1/2 - [READ] |

---

## [CRITICAL]

**C1. `requirements.txt` is corrupted; every Docker image build and CI run fails.** [RAN]
The last line, added by the commit "fix: add psycopg postgres driver to CI requirements", was written as UTF-16 into a UTF-8 file (bytes `p\0s\0y\0c\0...`). pip rejects it: `ERROR: Invalid requirement: 'p\x00s\x00y\x00...'`. `Dockerfile` runs `pip install -r requirements.txt`, so the api, celery_worker and celery_beat images cannot build, and the CI "Install dependencies" step fails.
*Fix:* replace the last line with plain ASCII `psycopg[binary]>=3.1` and save as UTF-8. Cause is almost certainly PowerShell `echo ... >> file`.
*Why psycopg is needed at all:* `requirements.txt` allows any SQLAlchemy; 2.1 maps `postgresql://` to the `psycopg` (v3) driver, but only `psycopg2-binary` was listed. Pinning `sqlalchemy>=2.0.28,<2.1` is the alternative fix.

**C2. Nothing creates the databases in a fresh stack.** [READ, confirmed by grep]
No `create_all`, Alembic, init SQL or bootstrap service exists. `main.py` lifespan only logs. After `docker-compose up`, Postgres has no `inventory`, `sales_ledger`, `retention_campaign_audit`, `user_risk_cache` or `golden_customers` tables, no seed inventory rows, Exasol has no raw/Golden tables, and the Exasol->Postgres JDBC connection (`POSTGRES_OMNISYNC`) does not exist. The first checkout returns 500, and the 5-minute sync fails.
*Fix:* a one-shot `bootstrap` compose service (or `scripts/bootstrap.py`) that runs `Base.metadata.create_all`, `scripts.apply_schemas`, a seed for `inventory`, `reverse_etl --setup`, and optionally `generate_data`.

**C3. Webhook hangs ~70 s and then lies when Redis is down.** [RAN]
With the broker unreachable, `POST /api/v1/webhooks/zendesk` took **71.5 s** (Celery retries the Redis result backend 20 times), then returned `202 queued` with a **synthetic task id**. The ticket was never enqueued: data loss that reports success. The handler is `async def` calling the blocking `.delay()`, so the whole event loop is blocked for that minute [READ]; Zendesk's delivery timeout is far shorter, so it will retry or disable the webhook.
*Fix:* make the handler a plain `def` (or `run_in_threadpool`), set short `broker_transport_options`/socket timeouts and `task_ignore_result=True`, and return **503** when enqueueing fails instead of a fake 202.

**C4. The Module 4 loop never runs end to end.** [READ, confirmed by grep]
`run_retention_vip_join` is not called by any Celery task, beat entry or endpoint. Tickets are staged with `STATUS='PENDING'` and nothing ever scores them, so no VIP campaign is ever dispatched or audited.

**C5. The retention "Golden JOIN" SQL will not execute on Exasol.** [READ; verify on a live Exasol]
`retention_engine.py` uses `LATERAL (SELECT churn_score FROM (EXECUTE SCRIPT ...PREDICT_CHURN_SCORE(...)))`. `EXECUTE SCRIPT` is a standalone statement, not usable in `FROM`, and the UDF is declared `SCALAR ... EMITS`, which is called in the select list. The tests pass only because they mock the connection.
*Fix:* declare the UDF `RETURNS DOUBLE` and write `SELECT TICKET_ID, ..., OMNISYNC_ANALYTICS.PREDICT_CHURN_SCORE(CLEANED_TEXT) AS CHURN_SCORE FROM ...`, then filter in an outer query.

**C6. Module 2's inventory sync is a stub, and nothing feeds it.** [READ]
`trigger_exasol_inventory_sync` (`celery_app.py:197`) only logs and returns `SYNC_COMPLETE`. `virtual_safety_buffer` therefore stays 0 forever, which makes the predictive 409 failsafe inert. Also, nothing calls `CartVelocityTracker.add_to_cart` or `get_cart_velocity`, and no task streams velocity to Exasol, so the Redis -> Exasol -> Postgres loop in the PRD does not exist yet.

---

## [HIGH]

**H1. Churn scores are not real.** [READ]
No trained NLP weights are in the repo (`backend/ml/weights/nlp_model/` holds only config and tokenizer files) and `scripts/upload_model_bucketfs.py` writes a **placeholder** `pytorch_model.bin`. In the UDF every load or inference failure is swallowed and emits `0.0`, so every ticket scores 0 and no VIP alert can fire, silently. If the load fails, each row retries the import and the HuggingFace-hub fallback name makes a network attempt per row.

**H2. UDF dependencies are not available in Exasol's default Python.** [UNVERIFIED]
Both UDFs import `lightgbm`/`pandas`/`torch`/`transformers`. Exasol's stock Python 3 environment does not include LightGBM, PyTorch or transformers; a custom script-language container uploaded to BucketFS is needed and nothing configures one.

**H3. Zendesk webhook signature is never verified.** [RAN]
`x_zendesk_signature` is accepted but unused, and `ZENDESK_WEBHOOK_SECRET` is never read. A request with a forged `x-zendesk-signature` header returned 202. Anyone can inject tickets.

**H4. Anonymous and email-only tickets, and the Golden Record is bypassed.** [READ + RAN]
An anonymous ticket (no `user_id`, no `email`) does **not crash**: it returns 202 and is staged with a NULL `USER_ID` [RAN]. But it then silently drops out of the INNER JOIN. Worse, the task falls back to storing the **email** in `USER_ID`, while LTV is keyed by Shopify user ids from `RAW_PURCHASES`, so email-only tickets never match. The join also ignores the stitched `GOLDEN_CUSTOMER_MEMBERS`, so a VIP whose spend is split across an account and devices is under-counted.
*Fix:* join ticket email -> `IDENTITY_NODES` (ZENDESK) -> golden customer, and take LTV from `GOLDEN_CUSTOMERS.TOTAL_SPEND_USD`.

**H5. Duplicate Zendesk deliveries and repeat campaigns.** [READ]
`STAGING_ZENDESK_TICKETS.TICKET_ID` is a primary key and the insert is not idempotent, so a redelivered ticket fails, triggers 3 retries, then fails (assuming Exasol enforces the key). `retention_campaign_audit` has no unique key on `ticket_id`, so re-running the join would dispatch the same VIP campaign again. *Fix:* `MERGE` on insert and a unique `(ticket_id)` for campaigns.

**H6. The "50 threads / last 5 items" OCC test does not test the OCC code.** [READ]
`test_high_concurrency_race_condition_50_threads_5_items` uses an in-process Python class with a `threading.Lock`; it never touches the endpoint, SQLAlchemy or Postgres. The endpoint tests run on SQLite, which cannot show row-lock behaviour. `endpoints.md` section 4 presents it as proof of the SQL logic. The OCC SQL itself is correct (see Step 2.4), but the hackathon criterion is not actually demonstrated. *Fix:* run the race against real Postgres (CI service container) through the real endpoint.

**H7. Level-5 "no Cash on Delivery" is not enforced.** [RAN]
A level-5 user checking out with `payment_method: "cod"` got `200 SUCCESS`. The risk level is read *after* the order commits, so it cannot block anything, and guests are looked up by `user_session_id`, which is not the device id the Golden Record uses, so they always get level 1. *Fix:* read the level before the OCC transaction and reject or flag COD for level 5.

**H8. Exasol container first-run and image pin.** [UNVERIFIED]
`exasol/docker-db:latest` is unpinned, and docker-db images normally need a one-time cluster initialisation before first start; compose just runs it. Check this the first time the container starts. Related: `depends_on` uses `service_started` for Exasol, which is up long before it accepts connections.

---

## [MEDIUM]

- **M1. Checkout loads the entire sales history of the SKU.** `Inventory.sales` is `lazy="selectin"`, so every `db.query(Inventory)` (and `db.refresh(item)`) also loads *all* `sales_ledger` rows for that item. Cost grows with sales volume, against the "no heavy work on the checkout path" rule. The `cascade="all, delete-orphan"` can also delete ledger rows, contradicting "immutable ledger" and the `RESTRICT` FK. Use `lazy="select"` (or `noload`) and drop the cascade. [READ]
- **M2. CI is red for a second reason:** `black --check` fails on `backend/models/sales.py` (extra blank lines). [RAN]
- **M3. Health endpoints are static strings.** `/health` and `/api/v1/health` return `HEALTHY` even when every database is down; `endpoints.md` says they validate subsystem availability. No container healthchecks for api/celery/exasol. [RAN/READ]
- **M4. `.env` is mandatory.** `env_file: .env` makes `docker-compose up` fail on a fresh clone, while the PRD promises a single command. Use `env_file: [{path: .env, required: false}]` or commit defaults. [RAN]
- **M5. Task time limits.** The global hard limit is 300 s, equal to the 5-minute schedule of the risk-score + export task, so a slow run is killed mid-flight. Give it a soft limit around 240 s. The three beat tasks have no retry or error handling (a failure is just logged; the next tick runs again). [READ]
- **M6. Two divergent Exasol connection helpers.** `core/exasol.py` uses `encryption=False`; `services/exasol_client.py` uses TLS with certificate checks off. Newer Exasol versions require TLS, so the staging path may fail. Consolidate. [READ]
- **M7. Three copies of the churn UDF** (`backend/db/exasol/02_nlp_churn_udf.sql`, `sql/01_pytorch_churn_udf.sql`, `backend/core/exasol_udf.py`) will drift. The `01` inventory script also does `CREATE OR REPLACE TABLE live_sku_telemetry` plus sample inserts, wiping data on re-run, and uses `TO_CHAR(..., 'ID')`, which should be checked against Exasol's format elements. [READ]
- **M8. `endpoints.md` overstates things:** "Zero Negative Stock Guarantee" (the PRD bans guarantee/hyperbole wording), OCC "validated" by the simulator, signature auth, health "verified readiness", Level-5 penalties, and a synthetic task id described as test-only when it is also returned in production on broker failure. [READ]
- **M9. SQL built with f-strings** in `retention_engine.get_golden_retention_join_sql` (thresholds interpolated, untyped). Safe with floats; unsafe if a caller ever passes strings. [READ]

**Indexes (your Step 3.2):** no critical index is missing. `sales_ledger` has `item_id`, `user_id`, `timestamp`; `retention_campaign_audit` has `ticket_id`, `user_id`, `created_at`; `inventory` and `user_risk_cache` use primary keys. Only gaps: a unique key on `retention_campaign_audit.ticket_id` (H5), an optional composite `(user_id, timestamp)` for LTV, and a redundant `index=True` on the `inventory` PK.

---

## [LOW / OPTIMIZATION]

- UDF 01 builds a pandas DataFrame and calls `predict` once per row; a `SET` script batching rows would be much faster.
- Redis cart keys (`cart:{item_id}`) have no TTL, so keys for dormant SKUs never expire (only pruned when read).
- `CORS allow_origins=["*"]` with `allow_credentials=True`; default secrets in `config.py` and `.env.example` (`API_SECRET_KEY`, a `sk_live_`-style placeholder).
- No `.dockerignore` (build context includes `.git` and any local data); all Python images run as root and each carries torch + transformers + weasyprint (multi-GB).
- `CheckoutResponse.status` documents `OCC_CONFLICT` / `RESERVED_SAFETY_BUFFER`, which are never returned (the endpoint raises HTTP 409); `CartItem` is unused.
- 18 non-blocking flake8 warnings; one task with complexity 13.
- `psycopg` (v3) and `psycopg2` are both required (the SQLAlchemy engine vs. the sync service/scripts).

---

## Step 2 - your four integration questions, answered

1. **Checkout with an empty `user_risk_cache`:** handled. A brand-new user returned `200` with level 1 [RAN], even when the table does not exist (the lookup catches the error, rolls back and uses level 1).
2. **Exasol -> Postgres sync run twice:** safe. Each run truncates the stage tables, then upserts with `ON CONFLICT DO UPDATE` and removes vanished keys in one transaction; an advisory lock makes an overlapping second run skip. No duplicates. [READ + mocked tests; **not run against real engines**.] The JDBC driver/connection must exist first (C2). The inventory half of "sync" is the stub in C6.
3. **Anonymous Zendesk ticket:** does not crash [RAN: 202, NULL `USER_ID` staged], but is silently excluded by the join (H4), and the join SQL itself is invalid (C5).
4. **OCC rollback:** `checkout.py` runs `db.rollback()` explicitly when the UPDATE matches 0 rows, before raising 409; the session stayed usable afterwards [RAN]. The predictive-buffer 409 does not need one (no writes happened; `get_db` closes the session).

## Step 3 - UDF memory/caching (your question 3)

Both UDFs cache the model in a module-level global (`_MODEL`), so on the success path it loads **once per Exasol VM, not per row** - correct. The risks are the failure path (reload attempt on every row, H1) and per-row DataFrame/tensor construction (LOW). Not run against real Exasol.

## Step 5 - `endpoints.md` verification

Every documented route exists and the status codes and JSON shapes match [RAN]: `GET /health`, `GET /api/v1/health`, `GET /`, `POST /api/v1/checkout` (200, 404, 409 buffer, 409 OCC, 422 x2), `POST /api/v1/webhooks/zendesk` (202, 422). **Doc/behaviour mismatches:** health "validates availability" (static, M3); signature header "authenticates" (not checked, H3); Level-5 penalties (not enforced, H7); OCC proof (simulator only, H6); synthetic task id (also returned on production broker failure, C3).

---

## What remains for the Streamlit frontend

`frontend/app.py` is 100% hardcoded mock data and never calls the API. To make it real it needs:
1. API read endpoints that do not exist yet: golden-customer lookup (by email/id), return risk by user, inventory list with buffers, VIP churn alerts (`retention_campaign_audit`).
2. Wiring each of its five pages to those endpoints plus `/api/v1/health`, and a checkout simulation form calling `POST /api/v1/checkout`.
3. Real figures for the metric cards (stitched identities, event counts) instead of constants.

## Caveats about the Module 3 code I wrote

Identity stitching, risk scoring and the `EXPORT ... INTO JDBC` sync have **never run on a real Exasol/Postgres**. They are covered by offline tests only. On generated data a pandas replica of the stitching scored pair precision 1.0 and recall 0.61 (devices that never log in or reveal an email cannot be linked by any signal). Levels 2 and 4 of the risk scale are proposed bands, not from the PRD.

## Suggested fix order (smallest effort, biggest unblock)
C1 (one line) -> M2 (black) -> C2 (bootstrap) -> C3 (webhook) -> C6/C4/C5 (Module 2 sync + Module 4 trigger and SQL) -> H3, H7 -> H1/H2 (real weights, script-language container) -> H6 (real-Postgres OCC test).
