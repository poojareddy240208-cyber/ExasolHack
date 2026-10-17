"""
OmniSync - Retail Analytics & Customer Success Intelligence Dashboard.
=======================================================================
Streamlit application serving customer success, inventory operations,
and merchandising teams.

Key Modules:
1. Golden Record Search (Probabilistic Identity Stitching)
2. Inventory Surge Monitor (Redis Cart Velocity -> Exasol LightGBM -> Postgres OCC)
3. Dynamic Return Friction (Pre-computed PyTorch Risk Levels 1-5)
4. NLP Churn Early Warning (Zendesk Webhooks -> PyTorch Sentiment + VIP LTV Join)
"""

import streamlit as st
import pandas as pd
import datetime

# ------------------------------------------------------------------------------
# Streamlit Page Configuration
# ------------------------------------------------------------------------------
st.set_page_config(
    page_title="OmniSync | Retail Intelligence Hub", page_icon="⚡", layout="wide", initial_sidebar_state="expanded"
)

# ------------------------------------------------------------------------------
# Custom Modern CSS Styling (Glassmorphism & Rich Aesthetic)
# ------------------------------------------------------------------------------
st.markdown(
    """
<style>
    /* Metric Card Modern Styling */
    .metric-card {
        background: linear-gradient(135deg, rgba(255, 255, 255, 0.05) 0%, rgba(255, 255, 255, 0.01) 100%);
        border: 1px solid rgba(255, 255, 255, 0.12);
        border-radius: 12px;
        padding: 20px;
        margin-bottom: 15px;
        box-shadow: 0 8px 24px rgba(0, 0, 0, 0.2);
    }
    .metric-value {
        font-size: 2.1rem;
        font-weight: 700;
        letter-spacing: -0.02em;
        margin-top: 4px;
        color: #00d2ff;
    }
    .metric-title {
        font-size: 0.85rem;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        color: #94a3b8;
    }
    .metric-delta {
        font-size: 0.85rem;
        font-weight: 600;
        color: #10b981;
    }
    .status-pill {
        display: inline-block;
        padding: 4px 12px;
        border-radius: 20px;
        font-size: 0.75rem;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.05em;
    }
    .status-healthy {
        background-color: rgba(16, 185, 129, 0.2);
        color: #34d399;
        border: 1px solid rgba(52, 211, 153, 0.4);
    }
    .status-alert {
        background-color: rgba(239, 68, 68, 0.2);
        color: #f87171;
        border: 1px solid rgba(248, 113, 113, 0.4);
    }
    .status-warning {
        background-color: rgba(245, 158, 11, 0.2);
        color: #fbbf24;
        border: 1px solid rgba(251, 191, 36, 0.4);
    }
</style>
""",
    unsafe_allow_html=True,
)

# ------------------------------------------------------------------------------
# Sidebar Navigation & System Status
# ------------------------------------------------------------------------------
with st.sidebar:
    st.markdown("## ⚡ **OmniSync Hub**")
    st.caption("Exasol In-Memory Intelligence & Hybrid OLTP Platform")
    st.divider()

    selected_nav = st.radio(
        "Navigation",
        [
            "🌟 Golden Record Search",
            "📦 Inventory Surge Monitor",
            "🔄 Dynamic Return Friction",
            "🚨 VIP Churn Early Warning",
            "⚙️ Architecture & Health",
        ],
        index=0,
    )

    st.divider()
    st.markdown("### 🖥️ **Live Infrastructure**")
    st.markdown(
        """
    - **OLAP**: <span class="status-pill status-healthy">Exasol (Privileged)</span><br>
    - **OLTP**: <span class="status-pill status-healthy">PostgreSQL 15</span><br>
    - **Cache**: <span class="status-pill status-healthy">Redis 7 Alpine</span><br>
    - **Sync**: <span class="status-pill status-healthy">Celery Beat</span>
    """,
        unsafe_allow_html=True,
    )
    st.caption(f"Last heartbeat: {datetime.datetime.now().strftime('%H:%M:%S UTC')}")


# ------------------------------------------------------------------------------
# Main Page View Routing
# ------------------------------------------------------------------------------

# 1. GOLDEN RECORD SEARCH
if selected_nav == "🌟 Golden Record Search":
    st.title("🌟 Probabilistic Identity Stitching: The Golden Record")
    st.write(
        "Merge anonymous guest sessions, checkout devices, and registered accounts without "
        "deterministic promo-code leakage. Behavioral web data is natively stored as JSON inside Exasol."
    )

    col1, col2, col3 = st.columns(3)
    with col1:
        st.markdown(
            """
        <div class="metric-card">
            <div class="metric-title">Stitched Identities</div>
            <div class="metric-value">148,920</div>
            <div class="metric-delta">↑ 12.4% this week</div>
        </div>
        """,
            unsafe_allow_html=True,
        )
    with col2:
        st.markdown(
            """
        <div class="metric-card">
            <div class="metric-title">Probabilistic Match Precision</div>
            <div class="metric-value">99.4%</div>
            <div class="metric-delta">Coupon Leak Defense Active</div>
        </div>
        """,
            unsafe_allow_html=True,
        )
    with col3:
        st.markdown(
            """
        <div class="metric-card">
            <div class="metric-title">Native JSON Events Stored</div>
            <div class="metric-value">4.2M</div>
            <div class="metric-delta">In-Memory Exasol Schema</div>
        </div>
        """,
            unsafe_allow_html=True,
        )

    st.subheader("🔍 Search Customer Golden Record")
    search_query = st.text_input("Enter Email, Anonymous Cookie ID, or Phone Number", "alex.morgan@retail-vip.com")

    st.markdown("#### Resolved Unified Profile")
    st.json(
        {
            "golden_customer_id": "CUST-GOLDEN-84920",
            "primary_email": "alex.morgan@retail-vip.com",
            "ltv_tier": "VIP_TOP_5_PERCENT",
            "total_spend_usd": 12450.00,
            "stitched_entities": [
                {"type": "registered_account", "id": "USR-9921", "confidence": 1.00},
                {"type": "mobile_app_cookie", "id": "DEV-IOS-A39281", "confidence": 0.98},
                {
                    "type": "guest_checkout_session",
                    "id": "SESS-WEB-44021",
                    "confidence": 0.92,
                    "promo_leak_discounted": True,
                },
            ],
            "native_exasol_behavioral_json": {
                "last_viewed_category": "Luxury Electronics",
                "active_cart_count": 2,
                "cart_velocity_5m": 3,
                "calculated_return_risk_level": 1,
            },
        }
    )


# 2. INVENTORY SURGE MONITOR
elif selected_nav == "📦 Inventory Surge Monitor":
    st.title("📦 Predictive Inventory & OCC Failsafe Monitor")
    st.write(
        "Redis monitors 5-minute cart velocity. Celery beat triggers Exasol LightGBM UDFs to forecast "
        "15-minute demand surges and syncs Virtual Safety Stock buffers back to PostgreSQL via `EXPORT INTO JDBC`."
    )

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.markdown(
            """
        <div class="metric-card">
            <div class="metric-title">Active 5m Carts</div>
            <div class="metric-value">1,482</div>
            <div class="metric-delta">Redis Rolling Window</div>
        </div>
        """,
            unsafe_allow_html=True,
        )
    with col2:
        st.markdown(
            """
        <div class="metric-card">
            <div class="metric-title">Predicted 15m Surge</div>
            <div class="metric-value">+3,240</div>
            <div class="metric-delta">LightGBM Exasol UDF</div>
        </div>
        """,
            unsafe_allow_html=True,
        )
    with col3:
        st.markdown(
            """
        <div class="metric-card">
            <div class="metric-title">OCC Collisions Mitigated</div>
            <div class="metric-value">312</div>
            <div class="metric-delta">Zero Negative Stock</div>
        </div>
        """,
            unsafe_allow_html=True,
        )
    with col4:
        st.markdown(
            """
        <div class="metric-card">
            <div class="metric-title">Added Checkout Latency</div>
            <div class="metric-value">0 ms</div>
            <div class="metric-delta">Pre-computed in Postgres</div>
        </div>
        """,
            unsafe_allow_html=True,
        )

    st.subheader("Live SKU Buffer & Concurrency Ledger")
    inventory_data = pd.DataFrame(
        [
            {
                "SKU": "SKU-4090-GPU",
                "Title": "NVIDIA RTX 4090 OC Edition",
                "Physical Stock": 10,
                "OCC Version": 42,
                "Active Carts (5m)": 85,
                "LightGBM Predicted Surge (15m)": 95,
                "Virtual Safety Stock Buffer": 170,
                "Failsafe Constraint": "CHECK (stock >= 0) ACTIVE",
            },
            {
                "SKU": "SKU-HERMES-BAG",
                "Title": "Hermès Birkin Handbag 30",
                "Physical Stock": 3,
                "OCC Version": 18,
                "Active Carts (5m)": 14,
                "LightGBM Predicted Surge (15m)": 12,
                "Virtual Safety Stock Buffer": 23,
                "Failsafe Constraint": "CHECK (stock >= 0) ACTIVE",
            },
            {
                "SKU": "SKU-AIR-JORDAN",
                "Title": "Air Jordan 1 Retro High OG",
                "Physical Stock": 50,
                "OCC Version": 104,
                "Active Carts (5m)": 28,
                "LightGBM Predicted Surge (15m)": 35,
                "Virtual Safety Stock Buffer": 13,
                "Failsafe Constraint": "CHECK (stock >= 0) ACTIVE",
            },
        ]
    )
    st.dataframe(inventory_data, use_container_width=True)

    st.info(
        "💡 **OCC Failsafe Logic**: PostgreSQL executes atomic conditional check:\n"
        "`UPDATE inventory SET stock = stock - :qty, version = version + 1 "
        "WHERE item_id = :id AND version = :read_version AND stock >= :qty;`"
    )


# 3. DYNAMIC RETURN FRICTION
elif selected_nav == "🔄 Dynamic Return Friction":
    st.title("🔄 Dynamic Return Friction Policy")
    st.write(
        "Exasol batch jobs run PyTorch models against the Golden Record to pre-compute 1-5 Risk Levels. "
        "PostgreSQL caches these scores, allowing the frontend to alter return friction with 0ms user latency."
    )

    c1, c2, c3 = st.columns(3)
    with c1:
        st.markdown(
            """
        <div class="metric-card">
            <div class="metric-title">Level 1: Free Returns</div>
            <div class="metric-value">&lt; 10% Risk</div>
            <div class="metric-delta">Reward: Instant QR Code</div>
        </div>
        """,
            unsafe_allow_html=True,
        )
    with c2:
        st.markdown(
            """
        <div class="metric-card">
            <div class="metric-title">Level 3: Friction Offer</div>
            <div class="metric-value">25 - 40%</div>
            <div class="metric-delta">Keep it for 10% Off Credit</div>
        </div>
        """,
            unsafe_allow_html=True,
        )
    with c3:
        st.markdown(
            """
        <div class="metric-card">
            <div class="metric-title">Level 5: Penalty Guard</div>
            <div class="metric-value">&gt; 60% Risk</div>
            <div class="metric-delta">$5 Restock Fee + No COD</div>
        </div>
        """,
            unsafe_allow_html=True,
        )

    st.subheader("Simulate User Dynamic Return Flow")
    user_type = st.selectbox(
        "Select User Profile to test dynamic policy injection:",
        ["VIP Customer (Level 1)", "Occasional Buyer (Level 3)", "Serial Wardrober / Flagged (Level 5)"],
    )

    if "Level 1" in user_type:
        st.success(
            "✅ **Policy Level 1 Applied (0ms latency)**: Seamless Free Return authorized. Prepaid label and instant store credit offered."
        )
    elif "Level 3" in user_type:
        st.warning(
            "⚠️ **Policy Level 3 Applied (0ms latency)**: Return Friction Active: 'Keep this item and enjoy an instant 10% refund bonus credited to your wallet!'"
        )
    else:
        st.error(
            "🚫 **Policy Level 5 Applied (0ms latency)**: High-Risk Serial Returner: $5 Restocking fee applies upon drop-off. Cash-on-delivery disabled for future orders."
        )


# 4. VIP CHURN EARLY WARNING
elif selected_nav == "🚨 VIP Churn Early Warning":
    st.title("🚨 NLP Churn Early Warning Radar")
    st.write(
        "Zendesk support webhooks stream into Celery queue. Exasol runs Transformer-based PyTorch UDFs "
        "to score ticket sentiment and joins `Churn Score >= 0.85` with `Customer LTV (Top 20%)` in native SQL."
    )

    st.markdown(
        """
    <div class="metric-card">
        <div class="metric-title">VIP Retention Interventions Triggered (Last 24h)</div>
        <div class="metric-value">24 High-Value Customers</div>
        <div class="metric-delta">Estimated $182,000 ARR Protected</div>
    </div>
    """,
        unsafe_allow_html=True,
    )

    churn_alerts = pd.DataFrame(
        [
            {
                "Ticket ID": "ZD-83921",
                "Customer": "Victoria Sterling",
                "LTV Tier": "Top 1% ($42,100)",
                "Sentiment Text Excerpt": "The stitching on this coat came undone on day two. This is totally unacceptable for a $2,400 garment...",
                "PyTorch Churn Score": 0.94,
                "Status": "ACTION TRIGGERED: Concierge Replacement",
            },
            {
                "Ticket ID": "ZD-83944",
                "Customer": "David Chang",
                "LTV Tier": "Top 10% ($11,800)",
                "Sentiment Text Excerpt": "Delayed shipment again. I'm moving all my corporate gifting to another vendor if this isn't resolved today.",
                "PyTorch Churn Score": 0.89,
                "Status": "ACTION TRIGGERED: Priority Escalation",
            },
        ]
    )
    st.dataframe(churn_alerts, use_container_width=True)


# 5. ARCHITECTURE & HEALTH
elif selected_nav == "⚙️ Architecture & Health":
    st.title("⚙️ OmniSync System Architecture")
    st.write("Strict separation of concerns between OLTP, OLAP, and Event Streaming.")

    st.code(
        """
  +-------------------------------------------------------------------------+
  |                           FastAPI Gateway                               |
  |             (OCC Checkout, Pre-computed Cache Reads, Webhooks)          |
  +--------------------+--------------------------------+-------------------+
                       |                                |
                       v                                v
        +------------------------------+  +-------------------------------+
        |    PostgreSQL 15 (OLTP)      |  |          Redis Alpine         |
        | - Product Catalog            |  | - Rolling 5m Cart Velocity    |
        | - OCC Inventory Ledgers      |  | - Celery Message Broker       |
        | - CHECK(stock >= 0)          |  +---------------+---------------+
        +--------------^---------------+                  |
                       |                                  v
                       | JDBC Export       +------------------------------+
                       | (Triggered by     |     Celery Beat & Worker     |
                       |  Celery Beat)     | - Asynchronous Zendesk Ingest|
                       |                   | - Batch Sync Triggering      |
                       |                   +--------------+---------------+
                       |                                  |
        +--------------+----------------------------------v---------------+
        |                  Exasol Personal Edition (OLAP)                 |
        |               *RUNS PRIVILEGED IN DOCKER (>= 8GB RAM)*          |
        | - In-Memory Analytics Engine                                    |
        | - BucketFS: PyTorch & LightGBM Model Weights                    |
        | - In-Database Python UDFs (Surge Prediction, Churn, Identity)   |
        | - Golden Record Unstructured JSON Store                         |
        +-----------------------------------------------------------------+
    """,
        language="text",
    )

    st.success("Platform ready. Run `docker-compose up -d --build` to deploy all containers.")
