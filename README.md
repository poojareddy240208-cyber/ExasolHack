# ⚡ OmniSync: Intelligent Retail Analytics & Concurrency Engine

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11](https://img.shields.io/badge/Python-3.11-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-009688.svg)](https://fastapi.tiangolo.com/)
[![Exasol](https://img.shields.io/badge/Exasol-In--Memory%20OLAP-004B87.svg)](https://www.exasol.com/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-15+-336791.svg)](https://www.postgresql.org/)
[![Redis](https://img.shields.io/badge/Redis-7%20Alpine-DC382D.svg)](https://redis.io/)
[![Celery](https://img.shields.io/badge/Celery-5.3+-37814A.svg)](https://docs.celeryq.dev/)
[![Streamlit](https://img.shields.io/badge/Streamlit-1.32+-FF4B4B.svg)](https://streamlit.io/)

> Built for the **Exasol Hackathon**: Bridging the gap between real-time transactional checkout and in-database machine learning intelligence.

---

## 📌 The Problem
Modern omnichannel retail brands lose tens of millions annually to four systemic operational blindspots:

1. **Identity Blindspots**: Fragmented guest checkouts, mobile app cookies, and distinct device IDs prevent brands from recognizing repeat customers, resulting in disconnected customer journeys.
2. **Ghost Stockouts & Overselling**: Concurrent flash sales trigger race conditions where inventory counts lag behind active shopping carts, leading to oversold stock and forced cancellations.
3. **Margin-Eroding Returns**: Blanket "free returns" policies encourage serial returners and wardrobing, drastically reducing margins without personalized friction or incentives.
4. **Silent VIP Churn**: High-value customers facing unresolved support tickets churn quietly weeks before traditional analytics flags their dissatisfaction.

---

## 💡 The Solution: Hybrid OLTP / OLAP Architecture

**OmniSync** resolves these challenges through a strict separation of concerns between transactional ledgers and in-memory analytics:

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

- **PostgreSQL (OLTP)**: Handles the catalog, customer checkout transactions, and strict **Optimistic Concurrency Control (OCC)** failsafes to eliminate overselling without database deadlocks.
- **Exasol Personal Edition (OLAP - "The Brain")**: Acts as the central in-memory intelligence engine. Models (**LightGBM** and **PyTorch**) are trained and executed directly inside Exasol via **Python UDFs**, storing model weights natively in **BucketFS**. No sensitive data leaves Exasol for inference.
- **Celery + Redis**: Ingests high-frequency webhook events asynchronously to prevent gateway timeouts, and orchestrates periodic batch data synchronization from Exasol to PostgreSQL.

---

## 🚀 Core Functional Modules

### 1. Identity Stitching (The Golden Record)
- Natively ingests unstructured JSON web-behavior events directly into Exasol.
- Runs probabilistic identity resolution to unify anonymous guest cookies and known user profiles.
- **Strict Anti-Leak Constraint**: *"Promo Code Leakage"* is treated purely as a weighted feature in a probabilistic model, **never** as a deterministic match, preventing coupon aggregator sites (e.g., Honey) from mistakenly merging thousands of distinct users.

### 2. Predictive Inventory & OCC Failsafe
- Redis continuously tracks active cart velocities within a rolling **5-minute window**.
- Celery beat periodically triggers Exasol's **LightGBM UDF** to forecast 15-minute demand surges and compute the **Virtual Safety Stock Buffer**:
  $$\text{Buffer} = \max(0, \text{Predicted\_Demand\_15m} - \text{Current\_Physical\_Stock} + \text{Active\_Carts\_5m})$$
- Celery beat executes an `EXPORT ... INTO JDBC` command from Exasol to update PostgreSQL.
- **Postgres OCC Concurrency Guard**:
  ```sql
  UPDATE inventory 
  SET stock = stock - :qty, version = version + 1 
  WHERE item_id = :id AND version = :read_version AND stock >= :qty;
  ```
  *(Postgres strictly enforces `CHECK (stock >= 0)` at the schema level).*

### 3. Dynamic Return Friction (0ms Latency Addition)
- An Exasol batch job runs a native PyTorch model against the Golden Record to pre-calculate Return Risk Levels:
  - **Level 1 (<10% return rate)**: Standard Free Return & instant prepaid label.
  - **Level 3 (25-40% return rate)**: Friction offer — *"Keep it for an instant 10% bonus credit"*.
  - **Level 5 (>60% return rate)**: Penalty guard — $5 Restocking fee warning and disable Cash on Delivery.
- Risk levels are pre-computed and stored in Postgres. The checkout and return UI reads these static integers, adding **0ms latency** to the live user experience.

### 4. NLP Churn Early Warning Radar
- Zendesk support webhooks drop payloads directly into Celery to eliminate API gateway timeouts.
- In-database PyTorch Transformer UDFs score customer ticket sentiment.
- A native Exasol SQL JOIN flags customers with **Churn Probability $\ge 0.85$** who are in the **Top 20% LTV (VIPs)**, dispatching alerts to retention marketing systems.

---

## 📋 System Prerequisites & Hardware Specs

- **Docker & Docker Compose**: Compose v2+ installed.
- **Host Memory**: **Minimum 8GB RAM** allocated to the Docker daemon (Exasol Personal Edition requires at least 4GB dedicated RAM).
- **Privileged Mode**: The Exasol Personal Edition container **MUST** run with the `--privileged` flag in Docker (already configured in `docker-compose.yml`).

---

## ⚡ Quickstart

### 1. Clone & Configure Environment
```bash
git clone https://github.com/poojareddy240208-cyber/ExasolHack.git
cd ExasolHack

# Copy the environment file
cp .env.example .env
```

### 2. Launch Entire Platform via Docker Compose
A single command builds and orchestrates FastAPI, PostgreSQL, Redis, Celery Worker, Celery Beat, Streamlit, and Exasol:

```bash
docker-compose up -d --build
```

### 3. Access Live Interfaces
| Service | URL | Description |
| :--- | :--- | :--- |
| **Streamlit Dashboard** | [http://localhost:8501](http://localhost:8501) | Customer Success & Analytics Hub |
| **FastAPI Swagger Docs** | [http://localhost:8000/docs](http://localhost:8000/docs) | Interactive API Explorer |
| **FastAPI Health Check** | [http://localhost:8000/health](http://localhost:8000/health) | Live Infrastructure Liveness Probe |
| **Exasol SQL Interface** | `localhost:8563` | Analytics Database Connection |
| **Exasol BucketFS** | `http://localhost:2580` | Native Model Weight Storage |

---

## 📂 Project Structure

```
ExasolHack/
├── .github/
│   └── workflows/
│       └── ci.yml             # GitHub Actions CI for flake8 and black
├── backend/
│   ├── api/
│   │   └── v1/
│   │       ├── endpoints/
│   │       │   ├── checkout.py# OCC transactional checkout endpoint
│   │       │   ├── health.py  # Health diagnostics
│   │       │   └── webhooks.py# Zendesk webhook ingestion endpoint
│   │       └── router.py      # APIRouter aggregation
│   ├── core/
│   │   └── config.py          # Pydantic v2 application settings
│   ├── schemas/
│   │   ├── checkout.py        # Pydantic checkout & OCC request/response schemas
│   │   └── webhook.py         # Pydantic Zendesk webhook models
│   ├── celery_app.py          # Celery worker tasks & periodic Beat schedule
│   ├── main.py                # FastAPI application entrypoint & middleware
│   └── __init__.py
├── frontend/
│   ├── app.py                 # Streamlit retail analytics dashboard
│   └── __init__.py
├── tests/
│   ├── __init__.py
│   └── test_occ_concurrency.py# Pytest suite simulating 50 concurrent threads
├── .env.example               # Environment template with credentials & ports
├── .gitignore                 # Standard Python, Node, Docker, weights & env ignores
├── Dockerfile                 # Multi-stage Python build for backend & Celery
├── Dockerfile.frontend        # Streamlit dashboard container build
├── docker-compose.yml         # 7-container unified stack configuration
├── LICENSE                    # MIT Open Source License
├── prd.md                     # Product Requirements Document & specifications
├── requirements.txt           # Verified Python dependencies
└── README.md                  # Comprehensive architectural documentation
```

---

## 🧪 Testing Concurrency & Race Conditions

To simulate high-concurrency race conditions (e.g., 50 threads attempting to purchase the last 5 items) and verify Optimistic Concurrency Control (OCC):

```bash
# Run the race-condition test suite
pytest tests/test_occ_concurrency.py -v
```

---

## 🛡️ Hackathon Rules of Engagement Adherence

1. **Zero External Model Training**: Models are trained inside Exasol UDFs; weights remain securely inside BucketFS.
2. **Factual Performance Constraints**: We guarantee 0ms checkout latency added through pre-computed risk integer lookups and strict Postgres `CHECK` constraints.
3. **Probabilistic Promo Resolution**: Never merge profiles on shared coupons alone.
4. **Passive Exasol Architecture**: Celery Beat serves as the active driver executing `EXPORT ... INTO JDBC`.

---

## 📄 License
This project is open-source and licensed under the [MIT License](LICENSE).