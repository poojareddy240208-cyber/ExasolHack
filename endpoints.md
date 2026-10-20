# ⚡ OmniSync API Specification & Endpoint Documentation

> **Single Source of Truth** for Frontend Developers, Integration Partners, and Hackathon Judges.  
> **Platform Version:** `v0.1.0` | **FastAPI Version:** `0.110+` | **Base URL:** `http://localhost:8000` | **API Prefix:** `/api/v1`

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
2. **Zero Negative Stock Guarantee:** Optimistic Concurrency Control (OCC) paired with a database constraint (`CHECK (stock >= 0)`) prevents overselling during high-concurrency flash sales without table-level deadlocks.
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
Container orchestrator liveness probe used by Docker Compose and Kubernetes to assess operational status across all hybrid infrastructure components (FastAPI application, PostgreSQL OLTP, Exasol OLAP, Redis cache, and Celery broker).

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
Provides a verified readiness check tailored for frontend clients and monitoring dashboards, validating individual subsystem availability.

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
6. **Dynamic Return Friction (0ms Added Latency):** Retrieves pre-computed return risk score (Levels 1 to 5) from PostgreSQL `user_risk_cache` via primary key. No analytics query is executed on the checkout path.

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
3. **Downstream Exasol PyTorch Pipeline:**
   - Ingests ticket descriptions into Exasol staging tables.
   - An in-database PyTorch Transformer UDF performs sentiment classification and generates a Churn Probability Score.
   - Native SQL query joins high-risk scores (**Churn Probability $\ge 0.85$**) with **Top 20% LTV VIP Customers** to dispatch priority retention triggers to customer success managers.

#### Request Headers
- `x-zendesk-signature` (*string, optional*): HMAC-SHA256 signature provided by Zendesk webhook configuration for source authentication.
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
  "message": "Zendesk ticket successfully enqueued for Exasol PyTorch NLP sentiment analysis."
}
```

##### Field Descriptions:
- `status` (*string*): Fixed to `"queued"` confirming Celery broker receipt.
- `task_id` (*string*): UUID of the registered Celery task (or deterministic synthetic ID if broker is running in unit-test mock mode).
- `ticket_id` (*string*): Echo of ingested Zendesk ticket ID.
- `message` (*string*): Informational confirmation string.

##### Error Responses:
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

## 🧪 4. Concurrency & Race Condition Verification

OmniSync's transactional checkout logic has been validated through rigorous automated tests (`tests/test_occ_concurrency.py`). 

### High-Concurrency Test Benchmark:
- **Test Case:** `test_high_concurrency_race_condition_50_threads_5_items`
- **Scenario:** 50 concurrent worker threads race simultaneously to purchase the last **5 units** of `FLASH-SALE-PS5`.
- **Observed Outcomes:**
  1. **Strictly 5 Transactions Succeeded** (`200 OK`), updating inventory version from `1` to `6`.
  2. **Strictly 45 Transactions Failed** (`409 Conflict`) due to OCC version mismatches or depleted stock.
  3. **Zero Negative Inventory:** Final physical stock settled at exactly `0`. Never dropped below zero (`CHECK (stock >= 0)`).

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

---

## 🔗 6. Interactive Documentation Links
When running the platform locally (`docker-compose up -d --build`):
- **Swagger UI (Interactive API Explorer):** [http://localhost:8000/docs](http://localhost:8000/docs)
- **ReDoc (Specification Reader):** [http://localhost:8000/redoc](http://localhost:8000/redoc)
- **OpenAPI Schema Definition:** [http://localhost:8000/openapi.json](http://localhost:8000/openapi.json)
