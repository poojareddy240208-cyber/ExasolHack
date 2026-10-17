# Part 1: Official Problem Statement (v2)

### **The Problem: The Cost of Fragmented Omnichannel Data**

Modern enterprise retailers operate in a highly fragmented digital ecosystem. Web stores, mobile apps, and customer support desks generate massive volumes of customer data, but these systems operate in isolated silos. This fragmentation creates four critical revenue leaks:

1. **Identity Blindspots:** A single customer is treated as multiple different entities across platforms, making true personalization and accurate Lifetime Value (LTV) calculation impossible.

2. **Reactive Inventory Failures (Ghost Stock-outs):** Traditional systems only register inventory changes at the point of sale. During high-traffic events, this lag allows multiple users to check out with the same physical item, resulting in cancelled orders and damaged brand trust.

3. **Margin-Eroding Returns:** Blanket "Free Returns" are weaponized by a small subset of "serial returners," severely eroding profit margins on otherwise successful sales.

4. **Silent VIP Churn:** High-value customers explicitly signal churn intent or product flaws in unstructured support tickets, but this data sits siloed from marketing databases, causing VIP attrition before an intervention can occur.

### **The OmniSync Solution**

OmniSync bridges the gap between transactional operations (OLTP) and deep analytics (OLAP). By positioning **Exasol** as the central in-memory intelligence engine, OmniSync ingests omnichannel data, stitches customer identities into a single "Golden Record," and trains/runs localized machine learning models directly inside the database via Python UDFs. By explicitly separating OLAP intelligence from OLTP processing, the backend can dynamically adjust inventory, alter return policies based on risk, and trigger automated win-back campaigns without adding latency to the checkout flow.

# Part 2: Product Requirements Document (PRD) v2

## 1. Technology Stack & Unified Nomenclature

To ensure high throughput and 0ms added latency at checkout, the architecture strictly separates OLTP from OLAP.

* **Analytics, ML & Data Warehousing (The Brain): Exasol Personal Edition**

  * *Features Used:* Python UDFs, BucketFS (for native model weight storage), native JSON processing.

  * *ML Strategy:* Models are trained directly inside Exasol using UDFs, and weights are saved directly to BucketFS. No data is exported to external servers for training or inference.

* **Transactional Backend & APIs: FastAPI (Python)**

* **Transactional Database (OLTP): PostgreSQL**

  * *Role:* Checkout ledgers, product catalog, and strictly enforced Optimistic Concurrency Control (OCC).

* **In-Memory Event Cache: Redis**

  * *Role:* Live cart velocity tracking.

* **Message Broker & Task Scheduler: Celery + Redis**

  * *Role:* Handles Zendesk webhooks asynchronously and orchestrates Exasol-to-Postgres data syncing via `Celery beat`.

## 2. Hardcoded Thresholds & Glossary

The system relies on the following standardized metrics across all modules:

* **Return Risk Levels:**

  * *Level 1:* <10% return rate (Reward: Standard Free Returns).

  * *Level 3:* 25-40% return rate (Friction: UI offer "Keep it for 10% off").

  * *Level 5:* >60% return rate (Penalty: \$5 Restocking Fee warning, no Cash on Delivery).

* **Churn Threshold:** Churn Probability Score `>= 0.85`.

* **VIP LTV Threshold:** Customer Lifetime Value in the **Top 20%** of all active users.

* **Cart Velocity Window:** Number of active 'Add to Carts' in the last **5 minutes**.

* **Virtual Safety Stock Buffer Formula:** `Buffer = MAX(0, Predicted_Demand_15m - Current_Physical_Stock + Active_Carts_5m)`

## 3. Functional Requirements (Core Modules)

**Module 1: Identity Stitching (The Golden Record)**

* **Requirement:** Merge anonymous guest data with known user profiles, storing unstructured JSON web-behavior natively in Exasol.

* **Constraint:** "Promo Code Leakage" must be treated as a **weighted feature** within a probabilistic matching model, NOT a deterministic match, to prevent coupon-site leaks from erroneously merging thousands of users.

**Module 2: Predictive Inventory**

* **Requirement:** Track cart velocity in Redis. `Celery beat` periodically streams this to Exasol. Exasol runs a LightGBM UDF to predict 15-minute demand surges.

* **Sync Logic:** `Celery beat` triggers an `EXPORT ... INTO JDBC` command from Exasol to push the updated Virtual Safety Stock back into PostgreSQL.

* **OCC Failsafe Logic:** The checkout must strictly use the following query to prevent negative inventory:
  `UPDATE inventory SET stock = stock - :qty, version = version + 1 WHERE item_id = :id AND version = :read_version AND stock >= :qty;`
  *(Postgres must also enforce a `CHECK (stock >= 0)` schema constraint).*

**Module 3: Dynamic Return Friction**

* **Requirement:** Execute an Exasol batch job (triggered by `Celery beat`) to run a PyTorch model against the Golden Record.

* **Logic:** Pre-compute the 1-5 Risk Levels. The FastAPI frontend alters the return UI strictly based on reading these pre-computed integers from Postgres (adding 0ms latency to the user session).

**Module 4: NLP Churn Early Warning**

* **Requirement:** Zendesk webhooks must drop payloads directly into a Celery queue to avoid API timeouts.

* **Logic:** Exasol ingests the text and runs a Transformer-Based Classifier (via PyTorch UDF) to extract sentiment. A native SQL `JOIN` connects the Churn Score (>= 0.85) with the user's LTV (Top 20%), triggering downstream marketing APIs.

## 4. Setup, Reproducibility & Open Source (Hackathon Criteria)

* **Hardware:** Minimum **8GB RAM** allocated to the Docker daemon.

* **Container Config:** The Exasol Personal Edition container MUST be run with the `--privileged` flag in Docker.

* **Quickstart:** A single `docker-compose up --build` must orchestrate FastAPI, Postgres, Redis, Celery, and Exasol.

* **Code Quality (5% Score):**

  * Must include an **MIT License**.

  * Must include **GitHub Actions CI/CD** for linting (`flake8`, `black`).

* **Testing:** Must include a dedicated `pytest` suite simulating high-concurrency race conditions (e.g., 50 threads attempting to buy the last 5 items) to prove the OCC logic.

## 5. Strict Rules of Engagement (Avoid at all costs)

* **NO External Model Training:** Do not export data out of Exasol to train models. Training and inference happen via UDFs; weights stay in BucketFS.

* **NO Hyperbolic Claims:** Avoid phrases like "mathematically guarantee", "sub-millisecond", or "zero latency." Use factual constraints (e.g., "0ms checkout latency added via pre-computation" and "strict `CHECK` constraints").

* **NO Deterministic Promo Matching:** Do not merge users based purely on a shared promo code.

* **NO Exasol Pushing/Scheduling:** Exasol is a passive DB. Do not write that "Exasol pushes" data. `Celery beat` must trigger the `EXPORT INTO JDBC` commands.