# ⚡ OmniSync API Specification & Endpoint Documentation

> **Single Source of Truth** for Frontend Developers, Integration Partners, and Hackathon Judges.  
> **Platform Version:** `v0.1.0` | **FastAPI Version:** `0.110+` | **Base URL:** `http://localhost:8000` | **API Prefix:** `/api/v1`  
> **Verified:** every route below is exercised by `tests/test_dashboard_endpoints.py`, which also fails if this table and the real routes ever differ.

---

## 🏛️ 1. Architecture Overview & Design Principles

OmniSync bridges the gap between high-throughput e-commerce checkout and in-database machine learning intelligence by strictly enforcing **OLTP and OLAP separation**:

```
               +-------------------------------------------------------+
               |                  FastAPI Gateway                      |
               |     (0ms added checkout latency, Webhook Ingest)      |
               +-------------------+--------------------+--------------+
                                   |                    |
                                   v                    v
     +-------------------------------+   +-----------------------------+
     |     PostgreSQL 15 (OLTP)      |   |        Redis Alpine         |
     |  * Product Catalog            |   |  * 5-min Cart Velocity      |
     |  * Strict OCC Ledgers         |   |  * Celery Task Broker       |
     |  * CHECK(stock >= 0)          |   +--------------+--------------+
     |  * Pre-computed Risk Cache    |                  |
     +---------------+---------------+                  v
                     ^                   +-----------------------------+
                     |                   |    Celery Worker & Beat     |
                     | EXPORT INTO JDBC  |  * Asynchronous Ingestion   |
                     | (Triggered by     |  * Periodic Sync Orchestrator
                     |  Celery Beat)     +--------------+--------------+
                     |                                  |
     +---------------+----------------------------------v--------------+
     |                 Exasol Personal Edition (OLAP)                  |
     |                   *The Central Brain (In-Memory)*               |
     |  * Python UDFs: LightGBM (Surge) & PyTorch (NLP / Return Risk)  |
     |  * BucketFS: In-Database Model Weight Repository                |
     |  * Native JSON Store: Unstructured Behavioral Web Analytics     |
     |  * Probabilistic Identity Stitching (Golden Record)             |
     +-----------------------------------------------------------------+
```

### Core Invariants:
1. **0ms Added Checkout Latency:** No heavy analytical queries or machine learning inference run in the transactional checkout path. Return risk levels (1–5) and safety buffers are pre-computed in Exasol and synced to PostgreSQL via Celery Beat for primary-key lookups.
2. **Strict Non-Negative Stock:** Optimistic Concurrency Control (OCC) paired with a database constraint (`CHECK (stock >= 0)`) prevents overselling during high-concurrency flash sales without table-level locks.
3. **Decoupled Asynchronous Ingestion:** External webhooks (e.g., Zendesk) return immediate `202 Accepted` acknowledgements to avoid API gateway timeouts, offloading Transformer NLP sentiment analysis to Celery workers and Exasol UDFs.
4. **Live Redis Cart Eviction:** On successful checkout, active user sessions are atomically removed from Redis Sorted Sets (`cart:{item_id}`) to maintain real-time cart velocity accuracy.

---

## 📑 2. Quick Reference Route Table

| Method | Endpoint Route | Module / Owner | Status Code | Primary Purpose |
| :--- | :--- | :--- | :--- | :--- |
| `GET` | `/health` | Diagnostics & System Health | `200 OK` | Container orchestrator liveness & infra probe |
| `GET` | `/api/v1/health` | Diagnostics & System Health | `200 OK` | Modular v1 health check with service breakdown |
| `POST` | `/api/v1/checkout` | Module 2 — Predictive Inventory & OCC Core | `200 OK` / `409` | Concurrency-safe order placement & cart eviction |
| `POST` | `/api/v1/webhooks/zendesk` | Module 4 — NLP Churn Early Warning Radar | `202 Accepted` | Support ticket ingest offloaded to Celery queue |
| `GET` | `/` | Diagnostics & Navigation | `200 OK` | Interactive documentation pointer (Swagger / ReDoc) |
| `GET` | `/api/v1/system/status` | Diagnostics & System Health | `200 OK` | Live PostgreSQL / Redis / Exasol connectivity, sync freshness, row counts |
| `GET` | `/api/v1/inventory` | Module 2 — Predictive Inventory | `200 OK` | All SKUs with stock, OCC version, safety buffer, live cart velocity |
| `GET` | `/api/v1/inventory/{item_id}` | Module 2 — Predictive Inventory | `200 OK` / `404` | One SKU |
| `POST` | `/api/v1/cart/add` | Module 2 — Predictive Inventory | `200 OK` / `404` / `503` | Record an add-to-cart (feeds Redis cart velocity) |
| `GET` | `/api/v1/cart/velocity/{item_id}` | Module 2 — Predictive Inventory | `200 OK` / `503` | Active carts for a SKU in the last 5 minutes |
| `GET` | `/api/v1/return-risk/policies` | Module 3 — Dynamic Return Friction | `200 OK` | The five friction levels and what each means |
| `GET` | `/api/v1/return-risk/{user_id}` | Module 3 — Dynamic Return Friction | `200 OK` | Pre-computed risk level (1-5) and policy for a shopper |
| `GET` | `/api/v1/golden-customers/stats` | Module 1 — Identity Stitching | `200 OK` | Headline Golden Record numbers |
| `GET` | `/api/v1/golden-customers` | Module 1 — Identity Stitching | `200 OK` | Search customers by email or any linked id |
| `GET` | `/api/v1/golden-customers/{golden_customer_id}` | Module 1 — Identity Stitching | `200 OK` / `404` | Full profile: stitched identities, LTV, return risk |
| `GET` | `/api/v1/golden-customers/{id}/investigation` | Module 1 — Identity Stitching | `200 OK` / `404` | Explainable AI (XAI) Identity Detective investigation evidence and confidence |
| `GET` | `/api/v1/churn/alerts` | Module 4 — NLP Churn Early Warning Radar | `200 OK` | Recent VIP churn alerts |
| `POST` | `/api/v1/churn/alerts/{ticket_id}/approve` | Module 4 — NLP Churn Early Warning Radar | `200 OK` / `404` | Approve and execute recommended VIP retention action |

---

## 🔍 3. Detailed Endpoint Specifications

---

### 3.1. System Health & Liveness Probe

#### Route & Method
`GET /health`

#### Module / Owner
**Diagnostics & System Health (Infrastructure Core)**  
*Source File:* `backend/main.py`

#### Description
Liveness probe: proves the FastAPI process is up. The `databases` values are static (`configured`), so this does **not** prove PostgreSQL, Exasol or Redis are reachable; use `GET /api/v1/system/status` for that.

#### Request Headers & Parameters
- **Query Parameters:** None
- **Request Body:** None

#### Response Formats

##### Success: `200 OK`
```json
{
  "status": "HEALTHY",
  "service": "OmniSync Core API",
  "version": "0.1.0",
  "environment": "development",
  "databases": {
    "oltp_postgres": "configured",
    "olap_exasol": "configured (privileged)",
    "cache_redis": "configured",
    "queue_celery": "configured"
  }
}
```

##### Field Descriptions:
- `status` (*string*): Overall health indicator (`"HEALTHY"`).
- `service` (*string*): Formal service identifier.
- `version` (*string*): Current platform build version.
- `environment` (*string*): Active environment (`development`, `staging`, `production`).
- `databases` (*object*): Connection state for PostgreSQL, Exasol (`--privileged` mode), Redis, and Celery.

---

### 3.2. API v1 Diagnostic Readiness Check

#### Route & Method
`GET /api/v1/health`

#### Module / Owner
**Diagnostics & System Health**  
*Source Files:* `backend/api/v1/router.py`, `backend/api/v1/endpoints/health.py`

#### Description
Static readiness response for frontend clients: it reports the configured services but does **not** contact them. For real connectivity, data-freshness and row-count checks use `GET /api/v1/system/status` (section 3.7).

#### Request Headers & Parameters
- **Query Parameters:** None
- **Request Body:** None

#### Response Formats

##### Success: `200 OK`
```json
{
  "status": "HEALTHY",
  "version": "0.1.0",
  "environment": "development",
  "services": {
    "api": "UP",
    "postgres_oltp": "CONFIGURED",
    "exasol_olap": "CONFIGURED (Privileged Docker)",
    "redis_cache": "CONFIGURED",
    "celery_workers": "READY"
  }
}
```

##### Pydantic Schema Model:
```python
class HealthResponse(BaseModel):
    status: str
    version: str
    environment: str
    services: Dict[str, str]
```

---

### 3.3. Transactional Checkout (OCC Protected & Predictive Failsafe)

#### Route & Method
`POST /api/v1/checkout`

#### Module / Owner
**Module 2 — Predictive Inventory & Transactional Core** *(with Module 3 Dynamic Return Friction)*  
*Source Files:* `backend/api/v1/endpoints/checkout.py`, `backend/schemas/checkout.py`

#### Description
Executes order placement in PostgreSQL with maximum concurrency and zero stock overselling. The endpoint coordinates three core safeguards:
1. **Catalog Existence & Stock Read:** Retrieves current inventory state and version stamp for `item_id`.
2. **Predictive Safety Stock Failsafe:**  
   $$\text{Available Stock} = \text{Current Physical Stock} - \text{Virtual Safety Buffer}$$
   The `virtual_safety_buffer` is dynamically updated by Exasol LightGBM surge predictions and active Redis 5-minute cart counts. If `quantity > available_stock`, checkout immediately aborts with `409 Conflict` before acquiring row updates.
3. **Optimistic Concurrency Control (OCC) Atomic Update:**  
   Executes the atomic conditional SQL query:
   ```sql
   UPDATE inventory
   SET stock = stock - :qty, version = version + 1
   WHERE item_id = :id AND version = :read_version AND stock >= :qty;
   ```
   If concurrent shoppers modify the inventory or deplete stock between the initial read and write, `rowcount == 0` is detected, triggering a transaction rollback and a `409 Conflict` response.
4. **Redis Cart Eviction:** On transaction commit, the user's session (`user_session_id`) is evicted from the item's Redis Sorted Set (`cart:{item_id}`) to update live cart velocity.
5. **Audit Logging:** Inserts an immutable order entry into PostgreSQL `sales_ledger`.
6. **Dynamic Return Friction (0ms Added Latency):** Retrieves the pre-computed return risk level (1 to 5) from PostgreSQL `user_risk_cache` via primary key and returns it as `precomputed_return_risk_level`. A shopper with no cached score gets level 1. No analytics query is executed on the checkout path. The level is returned for the UI to act on (see `GET /api/v1/return-risk/policies`); the server does **not** yet block Cash on Delivery for level 5.

#### Request Body
**Content-Type:** `application/json`

```json
{
  "item_id": "SKU-PS5-CONSOLE",
  "quantity": 2,
  "user_session_id": "sess_shopper_882",
  "user_id": "cust_vip_441",
  "expected_version": 1,
  "applied_promo_code": "SUMMER10",
  "payment_method": "credit_card"
}
```

##### Parameter Specifications (`CheckoutRequest` Schema):
| Parameter | Type | Required | Default | Description |
| :--- | :--- | :--- | :--- | :--- |
| `item_id` | `string` | **Yes** | — | Unique SKU or product identifier in PostgreSQL `inventory`. |
| `quantity` | `integer` | **Yes** | — | Units requested. Must be strictly greater than 0 (`gt=0`). |
| `user_session_id` | `string` | **Yes** | — | Active session/cookie UUID used for Redis cart tracking and post-purchase eviction. |
| `user_id` | `string` | No | `null` | Authenticated customer ID (defaults to `user_session_id` if omitted). |
| `expected_version` | `integer` | No | `null` | OCC version stamp from catalog view. If omitted, uses current DB read version. |
| `applied_promo_code` | `string` | No | `null` | Discount code. *Constraint: Treated as probabilistic feature, never deterministic user merge.* |
| `payment_method` | `string` | No | `"credit_card"` | Selected payment rail (e.g., `"credit_card"`, `"cod"`). |

#### Response Formats

##### Success: `200 OK`
```json
{
  "order_id": "ORD-3A4B8C1D",
  "status": "SUCCESS",
  "message": "Order successfully placed with OCC safety and stock integrity.",
  "item_id": "SKU-PS5-CONSOLE",
  "quantity": 2,
  "new_version": 2,
  "remaining_stock": 8,
  "items": [
    {
      "item_id": "SKU-PS5-CONSOLE",
      "quantity": 2,
      "success": true,
      "new_version": 2,
      "remaining_stock": 8,
      "error_message": null
    }
  ],
  "precomputed_return_risk_level": 1
}
```

##### Return Risk Level Thresholds (Module 3):
- **Level 1 (<10% returns):** Standard free returns; instant prepaid return shipping label.
- **Level 3 (25–40% returns):** Friction offer displayed in UI: *"Keep it for an instant 10% bonus credit"*.
- **Level 5 (>60% returns):** Penalty guard: $5 restocking fee warning and Cash on Delivery (COD) disabled.

##### Error Responses:

- **`404 Not Found` (Item Not In Catalog):**
  ```json
  {
    "detail": "Item 'SKU-DOES-NOT-EXIST' not found in product catalog."
  }
  ```

- **`409 Conflict` — Predictive Safety Buffer Triggered:**
  *Occurs when requested quantity exceeds `stock - virtual_safety_buffer` due to high projected surge demand.*
  ```json
  {
    "detail": "Item is in extremely high demand and currently reserved. Please try again in a few minutes."
  }
  ```

- **`409 Conflict` — OCC Version Collision / Insufficient Physical Stock:**
  *Occurs when another concurrent buyer purchased the stock or updated the version stamp during the transaction window.*
  ```json
  {
    "detail": "Concurrent transaction conflict. Another transaction updated this inventory version. Please retry."
  }
  ```

- **`422 Unprocessable Entity` — Payload Validation Error:**
  *Occurs if `quantity <= 0`, `item_id` is missing, or `user_session_id` is omitted.*
  ```json
  {
    "detail": [
      {
        "type": "greater_than",
        "loc": ["body", "quantity"],
        "msg": "Input should be greater than 0",
        "input": 0,
        "ctx": { "gt": 0 }
      }
    ]
  }
  ```

---

### 3.4. Zendesk Support Ticket Webhook Ingestion

#### Route & Method
`POST /api/v1/webhooks/zendesk`

#### Module / Owner
**Module 4 — NLP Churn Early Warning Radar**  
*Source Files:* `backend/api/v1/endpoints/webhooks.py`, `backend/schemas/webhook.py`

#### Description
High-throughput webhook receiver designed for customer support systems (Zendesk). To completely decouple external webhook delivery from database analytics and avoid gateway timeouts:
1. **Immediate Pydantic v2 Schema Validation:** Validates ticket structure and customer metadata.
2. **Asynchronous Celery Task Handoff:** Dispatches payload directly to Redis/Celery via `process_zendesk_ticket_task.delay(payload)`.
3. **Downstream Exasol Pipeline** (the VIP churn radar task runs every 5 minutes; see `GET /api/v1/churn/alerts`):
   - Ingests ticket descriptions into Exasol staging tables.
   - An in-database PyTorch Transformer UDF performs sentiment classification and generates a Churn Probability Score.
   - Native SQL query joins high-risk scores (**Churn Probability $\ge 0.85$**) with **Top 20% LTV VIP Customers** to dispatch priority retention triggers to customer success managers.

#### Request Headers
- `x-zendesk-signature` (*string, optional*): accepted but **not yet verified** (planned HMAC-SHA256 check). Until then, expose this endpoint only to trusted networks.
- `Content-Type`: `application/json`

#### Request Body
```json
{
  "ticket_id": "ZD-89211",
  "user_id": "cust_vip_441",
  "email": "sarah.connor@example.com",
  "subject": "Delayed shipment on high-priority order",
  "description": "My expedited order has not shipped after 4 days. If this is not resolved today, cancel my membership and refund my account.",
  "priority": "urgent",
  "status": "new",
  "created_at": "2026-10-19T13:45:00Z",
  "custom_fields": {
    "channel": "web_portal",
    "order_id": "ORD-3A4B8C1D"
  }
}
```

##### Parameter Specifications (`ZendeskTicketPayload` Schema):
| Parameter | Type | Required | Default | Description |
| :--- | :--- | :--- | :--- | :--- |
| `ticket_id` | `string` | **Yes** | — | Unique external identifier from Zendesk. |
| `subject` | `string` | **Yes** | — | Ticket summary line. |
| `description` | `string` | **Yes** | — | Raw ticket conversation/complaint text evaluated by PyTorch NLP. |
| `user_id` | `string` | No | `null` | Authenticated customer identifier (used for Golden Record stitch). |
| `email` | `string` | No | `null` | Customer contact email for identity resolution. |
| `priority` | `string` | No | `"normal"` | Priority level: `"low"`, `"normal"`, `"high"`, `"urgent"`. |
| `status` | `string` | No | `"new"` | Ticket lifecycle state (e.g., `"new"`, `"open"`, `"pending"`). |
| `created_at` | `datetime` | No | Current UTC | ISO-8601 creation timestamp. |
| `custom_fields` | `object` | No | `{}` | Key-value dictionary for supplemental tags and tracking attributes. |

#### Response Formats

##### Success: `202 Accepted`
```json
{
  "status": "queued",
  "task_id": "b78b0f92-5643-41bb-b09e-76e39266184a",
  "ticket_id": "ZD-89211",
  "message": "Payload dispatched to Celery worker for asynchronous NLP sentiment and churn analysis."
}
```

##### Field Descriptions:
- `status` (*string*): Fixed to `"queued"` confirming Celery broker receipt.
- `task_id` (*string*): UUID of the Celery task that was actually enqueued. A task id is only returned when the message reached the broker.
- `ticket_id` (*string*): Echo of ingested Zendesk ticket ID.
- `message` (*string*): Informational confirmation string.

##### Error Responses:
- **`503 Service Unavailable` — Broker Down:**
  *Occurs when the ticket cannot be enqueued (Redis unreachable or timing out). The response arrives within seconds and the ticket is **not** accepted, so Zendesk will retry.*
  ```json
  { "detail": "Message broker unavailable. Unable to enqueue support ticket." }
  ```

- **`422 Unprocessable Entity` — Missing Required Fields:**
  *Occurs if `ticket_id`, `subject`, or `description` are omitted.*
  ```json
  {
    "detail": [
      {
        "type": "missing",
        "loc": ["body", "ticket_id"],
        "msg": "Field required",
        "input": {}
      }
    ]
  }
  ```

---

### 3.5. Root API Index

#### Route & Method
`GET /`

#### Module / Owner
**Diagnostics & Navigation**  
*Source File:* `backend/main.py`

#### Description
Convenience entry point directing developers to interactive Swagger UI (`/docs`), ReDoc (`/redoc`), and health monitoring.

#### Response Formats

##### Success: `200 OK`
```json
{
  "message": "Welcome to the OmniSync Retail Analytics API. Visit /docs for Swagger UI.",
  "documentation": "/docs",
  "health": "/health"
}
```

---

### 3.6. Conventions for the dashboard (read-only) endpoints

The endpoints in sections 3.7 to 3.17 only **read** data. Nothing in them runs a model or an analytics query:

- Exasol computes the Golden Record, return risk and churn scores; Celery Beat copies the results to PostgreSQL every 5 minutes; these endpoints read the PostgreSQL copy (and Redis for live cart velocity).
- **`503 Service Unavailable`** with `{"detail": "Dashboard data is unavailable: the database is unreachable or its tables are not initialised. Run `python -m scripts.bootstrap` ..."}` is returned when PostgreSQL cannot be reached or a table does not exist yet. The UI should show an empty / "not synced yet" state.
- Lists that depend on Redis (`/inventory`) still answer `200` when Redis is down and set the live cart fields to `null`.
- Timestamps are ISO-8601. Money is USD. Ids are strings.

---

### 3.7. System Status (real service checks)

#### Route & Method
`GET /api/v1/system/status`  — *Source:* `backend/api/v1/endpoints/system.py`

#### Description
Contacts PostgreSQL, Redis and Exasol **in parallel** (about 3 s worst case when all are down), checks how fresh the Exasol → PostgreSQL sync is, and counts rows. Always answers `200`; `status` is `HEALTHY` or `DEGRADED` (a service is down, the last sync is older than 15 minutes, or nothing has synced yet).

#### Success: `200 OK`
```json
{
  "status": "HEALTHY",
  "version": "0.1.0",
  "environment": "development",
  "checked_at": "2026-10-22T17:05:11Z",
  "services": {
    "postgres_oltp": { "status": "UP", "latency_ms": 3.1, "detail": "SELECT 1 ok" },
    "redis_cache":   { "status": "UP", "latency_ms": 1.4, "detail": "PING ok" },
    "exasol_olap":   { "status": "UP", "latency_ms": 42.0, "detail": "SELECT 1 ok" }
  },
  "data_freshness": {
    "user_risk_cache_synced_at": "2026-10-22T17:02:00Z",
    "golden_customers_synced_at": "2026-10-22T17:02:00Z",
    "age_seconds": 191.0,
    "stale": false
  },
  "row_counts": { "inventory": 3, "sales_ledger": 120, "golden_customers": 5400, "user_risk_cache": 7100, "retention_campaign_audit": 12 }
}
```
A failed service shows `"status": "DOWN"` with the error in `detail`. A `null` row count means that table does not exist yet.

---

### 3.8. Inventory List

#### Route & Method
`GET /api/v1/inventory`  — *Source:* `backend/api/v1/endpoints/inventory.py`

#### Description
Every SKU with its physical stock, OCC version, the virtual safety buffer synced from Exasol, what checkout will allow (`stock − buffer`), and live carts from Redis. Ordered by `item_id`.

#### Success: `200 OK`
```json
{
  "generated_at": "2026-10-22T17:05:11Z",
  "total_items": 3,
  "total_active_carts_5m": 12,
  "redis_available": true,
  "items": [
    {
      "item_id": "SKU-4090-GPU",
      "name": "RTX 4090",
      "stock": 50,
      "version": 42,
      "virtual_safety_buffer": 20,
      "available_stock": 30,
      "active_carts_5m": 9,
      "predicted_demand_15m_estimate": 61
    }
  ]
}
```
- `available_stock = max(0, stock − virtual_safety_buffer)`.
- `predicted_demand_15m_estimate = buffer + stock − active_carts` when the buffer is above 0; **`null` when the buffer is 0**, because the buffer formula clamps at zero and the real prediction cannot be recovered.
- When Redis is down: `redis_available: false`, `total_active_carts_5m: null`, and each `active_carts_5m: null`.

#### Errors
`503` when the PostgreSQL tables are missing or unreachable.

---

### 3.9. Inventory Item

#### Route & Method
`GET /api/v1/inventory/{item_id}`

Same item shape as above. **`404`** `{"detail": "Item 'X' not found in product catalog."}` for an unknown SKU.

---

### 3.10. Add to Cart (feeds cart velocity)

#### Route & Method
`POST /api/v1/cart/add`  — *Source:* `backend/api/v1/endpoints/cart.py`

#### Description
Records that a shopper session has the item in a cart (Redis sorted set `cart:{item_id}`, scored by time). Before this endpoint existed nothing populated cart velocity, so the predictive buffer had no input. A successful checkout removes the session again.

#### Request Body
```json
{ "item_id": "SKU-4090-GPU", "user_session_id": "sess_shopper_882" }
```
Both fields are required (`422` otherwise).

#### Success: `200 OK`
```json
{ "item_id": "SKU-4090-GPU", "window_seconds": 300, "active_carts_5m": 10, "user_session_id": "sess_shopper_882" }
```
#### Errors
`404` unknown SKU · `503` Redis (or PostgreSQL) unavailable.

---

### 3.11. Cart Velocity

#### Route & Method
`GET /api/v1/cart/velocity/{item_id}`

#### Success: `200 OK`
```json
{ "item_id": "SKU-4090-GPU", "window_seconds": 300, "active_carts_5m": 10 }
```
Entries older than 5 minutes are pruned on read. `503` when Redis is unavailable.

---

### 3.12. Return Friction Policies

#### Route & Method
`GET /api/v1/return-risk/policies`  — *Source:* `backend/api/v1/endpoints/return_risk.py`, `backend/services/return_policy.py`

#### Description
The five levels and what each means for the shopper. **Levels 1, 3 and 5 are defined by the PRD; levels 2 and 4 are proposed** and flagged `defined_in_prd: false`.

#### Success: `200 OK` (levels 2 and 4 are also returned; abbreviated here)
```json
{
  "policies": [
    { "level": 1, "name": "Standard Free Returns", "return_rate_band": "< 10%", "defined_in_prd": true,
      "customer_message": "Free returns with an instant prepaid label.", "restocking_fee_usd": 0.0,
      "cash_on_delivery_allowed": true, "keep_it_discount_pct": 0 },
    { "level": 3, "name": "Return Friction Offer", "return_rate_band": "25% to < 40%", "defined_in_prd": true,
      "customer_message": "Keep this item and get an instant 10% credit instead of returning it.", "restocking_fee_usd": 0.0,
      "cash_on_delivery_allowed": true, "keep_it_discount_pct": 10 },
    { "level": 5, "name": "Penalty Guard", "return_rate_band": "> 60%", "defined_in_prd": true,
      "customer_message": "A $5 restocking fee applies to returns, and Cash on Delivery is not available.", "restocking_fee_usd": 5.0,
      "cash_on_delivery_allowed": false, "keep_it_discount_pct": 0 }
  ]
}
```

---

### 3.13. Return Risk for a Shopper

#### Route & Method
`GET /api/v1/return-risk/{user_id}`

#### Description
Reads `user_risk_cache` by primary key. `user_id` is a Shopify user id **or** a guest device id. If no score is cached yet, `found` is `false` and level 1 is returned (the same default checkout uses).

#### Success: `200 OK`
```json
{
  "user_id": "user_000005",
  "found": true,
  "risk_level": 5,
  "return_rate": 0.75,
  "purchases_6m": 8,
  "returns_6m": 6,
  "golden_customer_id": "CUST-GOLDEN-7F3A9C21B0",
  "computed_at": "2026-10-22T17:00:03Z",
  "synced_at": "2026-10-22T17:02:00Z",
  "policy": { "level": 5, "name": "Penalty Guard", "restocking_fee_usd": 5.0, "cash_on_delivery_allowed": false }
}
```
(`policy` is shown abbreviated; it carries every field listed in 3.12.) `503` when the cache table does not exist.

---

### 3.14. Golden Record — Headline Stats

#### Route & Method
`GET /api/v1/golden-customers/stats`  — *Source:* `backend/api/v1/endpoints/customers.py`

#### Success: `200 OK`
```json
{ "golden_customers": 5400, "stitched_nodes": 11800, "multi_identity_customers": 2900, "avg_members_per_customer": 2.185, "last_synced_at": "2026-10-22T17:02:00Z" }
```
`stitched_nodes` counts identity fragments (Shopify accounts, guest devices, Zendesk requesters); `multi_identity_customers` are customers made of more than one.

---

### 3.15. Golden Record — Search

#### Route & Method
`GET /api/v1/golden-customers?q={text}&limit={1-50, default 10}`

#### Description
`q` (2–255 characters) matches, case-insensitively: part of the primary email, the exact golden customer id, or the exact id of any linked identity (Shopify user id, guest device id, Zendesk requester email). Results are ordered by spend, highest first.

#### Success: `200 OK`
```json
{
  "query": "eve@example",
  "count": 1,
  "results": [
    { "golden_customer_id": "CUST-GOLDEN-7F3A9C21B0", "primary_email": "eve@example.com", "member_count": 4,
      "total_spend_usd": 12450.0, "first_seen_at": "2026-01-03T10:00:00Z", "last_seen_at": "2026-10-20T09:12:00Z",
      "ltv_tier": "Top 1% VIP" }
  ]
}
```
`422` if `q` is missing or shorter than 2 characters.

---

### 3.16. Golden Record — Profile

#### Route & Method
`GET /api/v1/golden-customers/{golden_customer_id}`

#### Description
One customer with every stitched identity, lifetime value rank and return risk.

#### Success: `200 OK`
```json
{
  "golden_customer_id": "CUST-GOLDEN-7F3A9C21B0",
  "primary_email": "eve@example.com",
  "member_count": 3,
  "total_spend_usd": 12450.0,
  "first_seen_at": "2026-01-03T10:00:00Z",
  "last_seen_at": "2026-10-20T09:12:00Z",
  "ltv_tier": "Top 1% VIP",
  "ltv_percent_rank": 0.998,
  "members": [
    { "node_id": "SHOPIFY:user_000005", "node_type": "SHOPIFY", "source_id": "user_000005", "confidence": 1.0 },
    { "node_id": "DEVICE:dev_aaa", "node_type": "DEVICE", "source_id": "dev_aaa", "confidence": 0.98 },
    { "node_id": "ZENDESK:eve@example.com", "node_type": "ZENDESK", "source_id": "eve@example.com", "confidence": 0.95 }
  ],
  "return_risk": { "user_id": "user_000005", "found": true, "risk_level": 5 }
}
```
- `confidence` is the strongest merge probability linking that identity to the customer (1.0 for a stand-alone or anchor account).
- `ltv_tier` ranks by spend: Top 1% / 5% / 10% / 20% VIP, otherwise `Standard` (the PRD's VIP threshold is the top 20%).
- `return_risk` (abbreviated here; same shape as 3.13) is `null` when none of the customer's accounts or devices has a cached score.

`404` for an unknown id.

---

### 3.17. VIP Churn Alerts

#### Route & Method
`GET /api/v1/churn/alerts?limit={1-200, default 50}&since_hours={1-2160, default 24}&min_score={0-1, default 0}`  — *Source:* `backend/api/v1/endpoints/churn.py`

#### Description
Alerts recorded by the scheduled VIP churn radar (churn score ≥ 0.85 joined with a Top 20% LTV golden customer), newest first. `count_in_window` counts alerts created in the last `since_hours` hours (use it for "interventions in the last 24h").

#### Success: `200 OK`
```json
{
  "since_hours": 24,
  "count_in_window": 2,
  "total_alerts": 12,
  "alerts": [
    { "ticket_id": "ZD-83921", "golden_customer_id": "CUST-GOLDEN-7F3A9C21B0", "customer_email": "eve@example.com",
      "churn_score": 0.97, "ltv_tier": "Top 1% VIP", "action_triggered": "ACTION TRIGGERED: Concierge Replacement",
      "status": "DISPATCHED", "created_at": "2026-10-22T16:05:00Z" }
  ]
}
```
`customer_email` is `null` until the Golden Record has been synced. The ticket text is not stored with the alert, so no excerpt is available. `422` for out-of-range parameters.

---

### 3.18. Which page uses which endpoint

| Dashboard page | Endpoints it needs |
| :--- | :--- |
| Architecture & Health | `GET /api/v1/system/status` |
| Golden Record Search | `GET /api/v1/golden-customers/stats`, `GET /api/v1/golden-customers?q=`, `GET /api/v1/golden-customers/{id}` |
| Inventory Surge Monitor | `GET /api/v1/inventory`, `POST /api/v1/cart/add`, `GET /api/v1/cart/velocity/{item_id}`, `POST /api/v1/checkout` |
| Dynamic Return Friction | `GET /api/v1/return-risk/policies`, `GET /api/v1/return-risk/{user_id}` |
| VIP Churn Early Warning | `GET /api/v1/churn/alerts`, `POST /api/v1/webhooks/zendesk` (demo ticket form) |

---

## 🧪 4. Concurrency & Race Condition Verification

OmniSync's checkout uses the OCC update plus the `CHECK (stock >= 0)` constraint. The automated race test (`tests/test_occ_concurrency.py`) is an **in-process simulation** of the OCC rule with a Python lock, not a run against PostgreSQL; a real multi-connection race against PostgreSQL is still to be added. 

### High-Concurrency Test Benchmark:
- **Test Case:** `test_high_concurrency_race_condition_50_threads_5_items` (simulation)
- **Scenario:** 50 concurrent worker threads race simultaneously to purchase the last **5 units** of `FLASH-SALE-PS5`.
- **Observed Outcomes:**
  1. **Strictly 5 Transactions Succeeded** (`200 OK`), updating inventory version from `1` to `6`.
  2. **Strictly 45 Transactions Failed** (`409 Conflict`) due to OCC version mismatches or depleted stock.
  3. **No Negative Inventory:** Final physical stock settled at exactly `0` and never dropped below zero in the simulation; in PostgreSQL this is additionally enforced by `CHECK (stock >= 0)`.

---

## 💻 5. Quickstart cURL Examples for Testing

### 1. Health Probe
```bash
curl -X GET "http://localhost:8000/health" -H "Accept: application/json"
```

### 2. Concurrency-Safe Checkout
```bash
curl -X POST "http://localhost:8000/api/v1/checkout" \
  -H "Content-Type: application/json" \
  -d '{
    "item_id": "SKU-PS5-CONSOLE",
    "quantity": 1,
    "user_session_id": "sess_judge_demo_001",
    "expected_version": 1
  }'
```

### 3. Zendesk Ticket Webhook Ingest
```bash
curl -X POST "http://localhost:8000/api/v1/webhooks/zendesk" \
  -H "Content-Type: application/json" \
  -d '{
    "ticket_id": "ZD-JUDGE-DEMO-99",
    "user_id": "user_vip_001",
    "email": "vip.shopper@example.com",
    "subject": "Delayed delivery on high-value item",
    "description": "I ordered this with expedited shipping and it has not shipped. Very frustrated.",
    "priority": "high"
  }'
```

### 4. Dashboard Reads
```bash
curl "http://localhost:8000/api/v1/system/status"
curl "http://localhost:8000/api/v1/inventory"
curl -X POST "http://localhost:8000/api/v1/cart/add" -H "Content-Type: application/json" \
  -d '{"item_id": "SKU-4090-GPU", "user_session_id": "sess_demo_1"}'
curl "http://localhost:8000/api/v1/return-risk/policies"
curl "http://localhost:8000/api/v1/return-risk/user_000005"
curl "http://localhost:8000/api/v1/golden-customers?q=eve@example"
curl "http://localhost:8000/api/v1/churn/alerts?since_hours=24"
```

---

## 🔗 6. Interactive Documentation Links
When running the platform locally (`docker-compose up -d --build`):
- **Swagger UI (Interactive API Explorer):** [http://localhost:8000/docs](http://localhost:8000/docs)
- **ReDoc (Specification Reader):** [http://localhost:8000/redoc](http://localhost:8000/redoc)
- **OpenAPI Schema Definition:** [http://localhost:8000/openapi.json](http://localhost:8000/openapi.json)
