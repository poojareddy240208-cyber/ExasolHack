"""
One-shot database bootstrap for a fresh OmniSync stack.

Without this, `docker-compose up` starts empty databases: the first checkout fails with "relation does not exist".

    python -m scripts.bootstrap               # PostgreSQL only (what the docker-compose `bootstrap` service runs)
    python -m scripts.bootstrap --exasol      # also set up Exasol (run once Exasol has finished booting)
    python -m scripts.bootstrap --localhost   # when running on your own machine instead of inside a container

PostgreSQL:
  1. SQLAlchemy models -> `inventory`, `sales_ledger`, `retention_campaign_audit`
  2. sql/postgres/*.sql -> `users`, `purchases`, `returns`, `support_tickets`, `golden_customers`,
     `user_risk_cache` and their stage tables
  3. seed the three demo inventory items (only when the table is empty)
Exasol (--exasol):
  4. sql/exasol/*.sql -> schemas and tables
  5. the LightGBM demand UDF and the PyTorch churn UDF
  6. the CONNECTION object used by `EXPORT ... INTO JDBC`

Every step is idempotent, so running the script again is safe.
"""

import argparse
import logging
import re
from pathlib import Path
from typing import List, Optional

from backend.core.config import settings
from backend.db.session import SessionLocal, engine
from backend.models.base import Base

# Importing the models registers their tables with SQLAlchemy's metadata.
from backend.models.inventory import Inventory, InventoryReservation  # noqa: F401
from backend.models.sales import (
    RetentionCampaignAudit,
    SalesLedger,
    UserRiskCache,
)  # noqa: F401
from scripts.apply_schemas import apply_exasol, apply_postgres

logger = logging.getLogger("omnisync.bootstrap")

UDF_FILES = [
    Path(__file__).resolve().parent.parent
    / "backend"
    / "db"
    / "exasol"
    / "01_inventory_predict_udf.sql",
    Path(__file__).resolve().parent.parent
    / "backend"
    / "db"
    / "exasol"
    / "02_nlp_churn_udf.sql",
]

SEED_ITEMS = [
    {"item_id": "SKU-4090-GPU", "name": "RTX 4090", "stock": 50},
    {"item_id": "SKU-HERMES-BAG", "name": "Hermes Birkin", "stock": 5},
    {"item_id": "SKU-AIR-JORDAN", "name": "Air Jordan 1", "stock": 150},
]


def seed_inventory() -> int:
    """Insert the demo inventory items when the table is empty. Returns how many rows were added."""
    db = SessionLocal()
    try:
        if db.query(Inventory).count() > 0:
            logger.info("Inventory already has rows, skipping seed.")
            return 0
        db.add_all(
            [
                Inventory(version=1, virtual_safety_buffer=0, **item)
                for item in SEED_ITEMS
            ]
        )
        db.commit()
        logger.info("Seeded %d inventory items.", len(SEED_ITEMS))
        return len(SEED_ITEMS)
    finally:
        db.close()


def udf_statements(sql_path: Path) -> List[str]:
    """Extract the `--/ ... /` script blocks (CREATE ... SCRIPT) from an Exasol SQL file, without the markers."""
    text = sql_path.read_text(encoding="utf-8")
    return [
        block.strip()
        for block in re.findall(r"^--/\s*\n(.*?)^/\s*$", text, flags=re.S | re.M)
    ]


def setup_exasol(host: Optional[str]) -> None:
    """Create Exasol schemas/tables, register the UDFs and create the JDBC connection to PostgreSQL."""
    from backend.services.exasol_client import connect_exasol
    from backend.services.reverse_etl import SCHEMA, create_exasol_connection

    apply_exasol(host or settings.EXASOL_HOST)
    conn = connect_exasol(host=host, schema=SCHEMA)
    try:
        for path in UDF_FILES:
            for statement in udf_statements(path):
                conn.execute(statement)
            logger.info("Registered UDFs from %s", path.name)
    finally:
        conn.close()
    create_exasol_connection(host=host)


def seed_xai_demo_data() -> None:
    """
    Seed hardcoded Hackathon Demo Scenarios for Phase 2:
    1. Return Scenarios:
       - Customer A (Unlucky VIP): 6 returns, 5 with DEFECTIVE_BATCH and supplier_batch_id='BATCH-8992',
         1 with WRONG_SIZE -> evaluates to Risk Level 1 with 83% defect rationale.
       - Customer B (Wardrober): 4 returns, all 4 with WARDROBING_SUSPECTED -> evaluates to Risk Level 5
         with 100% abuse rationale and human review required.
    2. Identity Scenario (DEMO-EVE-001):
       - Anonymous laptop (Cart Abandon), anonymous iPhone (Promo Code), and Eve's VIP record
         seeded into identity_evidence and candidate_links to yield a 95% confidence score with
         supporting and counter evidence strings.
    """
    db = SessionLocal()
    try:
        from datetime import datetime, timezone, timedelta
        from sqlalchemy import text
        import uuid

        # Ensure dashboard tables exist even in environments where raw SQL wasn't executed
        db.execute(text("""
            CREATE TABLE IF NOT EXISTS golden_customers (
                golden_customer_id TEXT PRIMARY KEY,
                primary_email TEXT,
                member_count INTEGER NOT NULL,
                total_spend_usd NUMERIC(12, 2) NOT NULL DEFAULT 0,
                first_seen_at TIMESTAMPTZ,
                last_seen_at TIMESTAMPTZ,
                synced_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """))
        db.execute(text("""
            CREATE TABLE IF NOT EXISTS golden_customer_members (
                golden_customer_id TEXT NOT NULL,
                node_id TEXT NOT NULL,
                node_type TEXT NOT NULL,
                source_id TEXT NOT NULL,
                confidence NUMERIC(5, 4) NOT NULL,
                synced_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (golden_customer_id, node_id)
            )
        """))
        db.execute(text("""
            CREATE TABLE IF NOT EXISTS identity_evidence (
                source_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                confidence_weight NUMERIC(5, 4) NOT NULL,
                linked_to_golden_id TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """))
        db.execute(text("""
            CREATE TABLE IF NOT EXISTS candidate_links (
                node_a TEXT NOT NULL,
                node_b TEXT NOT NULL,
                model_score NUMERIC(5, 4) NOT NULL,
                human_review_status TEXT NOT NULL DEFAULT 'PENDING',
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """))
        db.commit()

        now = datetime.now(timezone.utc)

        # -------------------------------------------------------------
        # 1. Return Scenarios: Customer A & Customer B
        # -------------------------------------------------------------
        cust_a_count = (
            db.query(SalesLedger)
            .filter(SalesLedger.user_id.in_(["Customer A", "customer_a"]))
            .count()
        )
        if cust_a_count == 0:
            demo_item = "SKU-AIR-JORDAN"
            for i in range(5):
                db.add(
                    SalesLedger(
                        transaction_id=uuid.uuid4(),
                        item_id=demo_item,
                        user_id="Customer A",
                        quantity=1,
                        timestamp=now - timedelta(days=20 - i),
                        return_reason_code="DEFECTIVE_BATCH",
                        supplier_batch_id="BATCH-8992",
                    )
                )
            db.add(
                SalesLedger(
                    transaction_id=uuid.uuid4(),
                    item_id=demo_item,
                    user_id="Customer A",
                    quantity=1,
                    timestamp=now - timedelta(days=2),
                    return_reason_code="WRONG_SIZE",
                    supplier_batch_id=None,
                )
            )
            logger.info(
                "Seeded Customer A (Unlucky VIP) with 6 returns (5 DEFECTIVE_BATCH BATCH-8992)."
            )

        cust_b_count = (
            db.query(SalesLedger)
            .filter(SalesLedger.user_id.in_(["Customer B", "customer_b"]))
            .count()
        )
        if cust_b_count == 0:
            demo_item = "SKU-HERMES-BAG"
            for i in range(4):
                db.add(
                    SalesLedger(
                        transaction_id=uuid.uuid4(),
                        item_id=demo_item,
                        user_id="Customer B",
                        quantity=1,
                        timestamp=now - timedelta(days=15 - i * 3),
                        return_reason_code="WARDROBING_SUSPECTED",
                        supplier_batch_id=None,
                    )
                )
            logger.info(
                "Seeded Customer B (Wardrober) with 4 returns (WARDROBING_SUSPECTED)."
            )

        db.commit()

        # Seed user_risk_cache for Customer A and Customer B
        for user_id, level, rate, p6m, r6m in [
            ("Customer A", 1, 0.8333, 6, 6),
            ("Customer B", 5, 1.0000, 4, 4),
        ]:
            existing = db.execute(
                text("SELECT user_id FROM user_risk_cache WHERE user_id = :uid"),
                {"uid": user_id},
            ).fetchone()
            if not existing:
                try:
                    db.execute(
                        text("""
                            INSERT INTO user_risk_cache (user_id, golden_customer_id, risk_level, return_rate, purchases_6m, returns_6m, computed_at, synced_at)
                            VALUES (:uid, :gid, :lvl, :rate, :p6m, :r6m, :now, :now)
                        """),
                        {
                            "uid": user_id,
                            "gid": f"GOLDEN-{user_id.replace(' ', '-')}",
                            "lvl": level,
                            "rate": rate,
                            "p6m": p6m,
                            "r6m": r6m,
                            "now": now,
                        },
                    )
                except Exception:
                    pass
        db.commit()

        # -------------------------------------------------------------
        # 2. Identity Scenario: DEMO-EVE-001
        # -------------------------------------------------------------
        eve_golden_id = "DEMO-EVE-001"
        existing_eve = db.execute(
            text(
                "SELECT golden_customer_id FROM golden_customers WHERE golden_customer_id = :id"
            ),
            {"id": eve_golden_id},
        ).fetchone()

        if not existing_eve:
            db.execute(
                text("""
                    INSERT INTO golden_customers (golden_customer_id, primary_email, member_count, total_spend_usd, first_seen_at, last_seen_at, synced_at)
                    VALUES (:id, :email, :members, :spend, :first, :last, :synced)
                """),
                {
                    "id": eve_golden_id,
                    "email": "eve.vip@example.com",
                    "members": 3,
                    "spend": 4850.00,
                    "first": now - timedelta(days=90),
                    "last": now,
                    "synced": now,
                },
            )
            members_data = [
                (eve_golden_id, "SHOPIFY:eve_vip_001", "SHOPIFY", "eve_vip_001", 1.0),
                (
                    eve_golden_id,
                    "DEVICE:laptop_anon_eve",
                    "DEVICE",
                    "laptop_anon_eve",
                    0.95,
                ),
                (
                    eve_golden_id,
                    "DEVICE:iphone_anon_eve",
                    "DEVICE",
                    "iphone_anon_eve",
                    0.95,
                ),
            ]
            for gid, nid, ntype, sid, conf in members_data:
                db.execute(
                    text("""
                        INSERT INTO golden_customer_members (golden_customer_id, node_id, node_type, source_id, confidence, synced_at)
                        VALUES (:gid, :nid, :ntype, :sid, :conf, :synced)
                    """),
                    {
                        "gid": gid,
                        "nid": nid,
                        "ntype": ntype,
                        "sid": sid,
                        "conf": conf,
                        "synced": now,
                    },
                )
            logger.info("Seeded DEMO-EVE-001 golden customer profile and members.")

        # Seed candidate_links for DEMO-EVE-001
        existing_link = db.execute(
            text("SELECT node_a FROM candidate_links WHERE node_a = :id"),
            {"id": eve_golden_id},
        ).fetchone()
        if not existing_link:
            db.execute(
                text("""
                    INSERT INTO candidate_links (node_a, node_b, model_score, human_review_status, created_at)
                    VALUES (:na, :nb, :score, :status, :created)
                """),
                {
                    "na": eve_golden_id,
                    "nb": "DEVICE:laptop_anon_eve",
                    "score": 0.95,
                    "status": "STRONG_CANDIDATE_PENDING_REVIEW",
                    "created": now,
                },
            )
            db.execute(
                text("""
                    INSERT INTO candidate_links (node_a, node_b, model_score, human_review_status, created_at)
                    VALUES (:na, :nb, :score, :status, :created)
                """),
                {
                    "na": eve_golden_id,
                    "nb": "DEVICE:iphone_anon_eve",
                    "score": 0.95,
                    "status": "STRONG_CANDIDATE_PENDING_REVIEW",
                    "created": now,
                },
            )
            logger.info("Seeded candidate_links for DEMO-EVE-001 (95% confidence).")

        # Seed identity_evidence for DEMO-EVE-001
        existing_evidence = db.execute(
            text(
                "SELECT source_id FROM identity_evidence WHERE linked_to_golden_id = :id"
            ),
            {"id": eve_golden_id},
        ).fetchone()
        if not existing_evidence:
            evidence_data = [
                (
                    "DEVICE:laptop_anon_eve",
                    "Identical specific SKU search intent",
                    0.95,
                    eve_golden_id,
                ),
                (
                    "DEVICE:iphone_anon_eve",
                    "Leaked VIP Promo Code EVES_VIP_2026",
                    0.95,
                    eve_golden_id,
                ),
                (
                    "DEVICE:laptop_anon_eve",
                    "Cart Abandon: High-value designer item abandoned in guest session",
                    0.90,
                    eve_golden_id,
                ),
                (
                    "SHOPIFY:eve_vip_001",
                    "VIP Customer Profile Match",
                    1.0,
                    eve_golden_id,
                ),
                (
                    "DEVICE:laptop_anon_eve",
                    "No authenticated login",
                    -0.05,
                    eve_golden_id,
                ),
                (
                    "DEVICE:iphone_anon_eve",
                    "Promo code could be shared via public forum",
                    -0.10,
                    eve_golden_id,
                ),
            ]
            for sid, etype, cweight, lgid in evidence_data:
                db.execute(
                    text("""
                        INSERT INTO identity_evidence (source_id, event_type, confidence_weight, linked_to_golden_id, created_at)
                        VALUES (:sid, :etype, :cweight, :lgid, :created)
                    """),
                    {
                        "sid": sid,
                        "etype": etype,
                        "cweight": cweight,
                        "lgid": lgid,
                        "created": now,
                    },
                )
            logger.info("Seeded identity_evidence for DEMO-EVE-001.")

        # -------------------------------------------------------------
        # 3. NLP Demo Tickets: Fit Issue & The Ruined Event
        # -------------------------------------------------------------
        try:
            db.execute(text("""
                CREATE TABLE IF NOT EXISTS support_tickets (
                    ticket_id TEXT PRIMARY KEY,
                    user_id TEXT,
                    requester_email TEXT,
                    subject TEXT NOT NULL,
                    description TEXT NOT NULL,
                    priority TEXT NOT NULL DEFAULT 'normal',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """))
            db.commit()
        except Exception:
            pass

        ticket_1_id = "ZD-DEMO-FIT-001"
        existing_t1 = (
            db.query(RetentionCampaignAudit)
            .filter(RetentionCampaignAudit.ticket_id == ticket_1_id)
            .first()
        )
        if not existing_t1:
            db.add(
                RetentionCampaignAudit(
                    audit_id=uuid.uuid4(),
                    ticket_id=ticket_1_id,
                    user_id="DEMO-EVE-001",
                    churn_score=0.30,
                    ltv_tier="Top 1% VIP",
                    action_triggered="AUTO_RETURN_LABEL",
                    status="PENDING_REVIEW",
                    created_at=now - timedelta(hours=3),
                    urgency="MEDIUM",
                    root_cause="PRODUCT_PREFERENCE",
                    xai_explanation="Product preference issue detected. Customer dissatisfied with item style, color, or variant. Automated prepaid return label recommended.",
                )
            )

        ticket_2_id = "ZD-DEMO-WEDDING-002"
        existing_t2 = (
            db.query(RetentionCampaignAudit)
            .filter(RetentionCampaignAudit.ticket_id == ticket_2_id)
            .first()
        )
        if not existing_t2:
            db.add(
                RetentionCampaignAudit(
                    audit_id=uuid.uuid4(),
                    ticket_id=ticket_2_id,
                    user_id="DEMO-EVE-001",
                    churn_score=0.99,
                    ltv_tier="Top 1% VIP",
                    action_triggered="ESCALATE_HUMAN_CONCIERGE_AND_OVERNIGHT_REPLACEMENT",
                    status="PENDING_REVIEW",
                    created_at=now - timedelta(hours=1),
                    urgency="CRITICAL",
                    root_cause="FULFILLMENT_FAILURE",
                    xai_explanation="Critical urgency detected due to fulfillment failure. Standard discount insufficient. Human concierge escalation required.",
                )
            )

        try:
            t1_support = db.execute(
                text("SELECT ticket_id FROM support_tickets WHERE ticket_id = :id"),
                {"id": ticket_1_id},
            ).fetchone()
            if not t1_support:
                db.execute(
                    text("""
                        INSERT INTO support_tickets (ticket_id, user_id, requester_email, subject, description, priority, created_at)
                        VALUES (:tid, :uid, :email, :subj, :desc, :pri, :created)
                    """),
                    {
                        "tid": ticket_1_id,
                        "uid": "DEMO-EVE-001",
                        "email": "eve.vip@example.com",
                        "subj": "Fit and Color Mismatch Return",
                        "desc": "I hate the color of this shirt, it looks nothing like the picture. I want to return it.",
                        "pri": "normal",
                        "created": now - timedelta(hours=3),
                    },
                )
            t2_support = db.execute(
                text("SELECT ticket_id FROM support_tickets WHERE ticket_id = :id"),
                {"id": ticket_2_id},
            ).fetchone()
            if not t2_support:
                db.execute(
                    text("""
                        INSERT INTO support_tickets (ticket_id, user_id, requester_email, subject, description, priority, created_at)
                        VALUES (:tid, :uid, :email, :subj, :desc, :pri, :created)
                    """),
                    {
                        "tid": ticket_2_id,
                        "uid": "DEMO-EVE-001",
                        "email": "eve.vip@example.com",
                        "subj": "Wedding Dress Not Shipped - Emergency",
                        "desc": "I ordered this dress for my wedding tomorrow and it hasn't shipped! Cancel my account immediately, you ruined everything!",
                        "pri": "urgent",
                        "created": now - timedelta(hours=1),
                    },
                )
        except Exception:
            pass

        db.commit()
        logger.info("Seeded demo NLP tickets for Top 1% VIP DEMO-EVE-001.")
    finally:
        db.close()


def init_db(host: Optional[str] = None, with_exasol: bool = False) -> None:
    """Run the full bootstrap. `host` overrides the database hostnames (e.g. "localhost")."""
    logger.info("Creating PostgreSQL tables from the SQLAlchemy models...")
    Base.metadata.create_all(bind=engine)
    logger.info("Applying PostgreSQL Module 3 schema (sql/postgres)...")
    apply_postgres(host or settings.POSTGRES_HOST)
    seed_inventory()
    seed_xai_demo_data()
    if with_exasol:
        setup_exasol(host)
    logger.info("Bootstrap complete.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Bootstrap the OmniSync databases.")
    parser.add_argument(
        "--exasol",
        action="store_true",
        help="also set up Exasol (schemas, UDFs, JDBC connection)",
    )
    parser.add_argument(
        "--localhost",
        action="store_true",
        help="connect to localhost instead of docker hostnames",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    init_db(host="localhost" if args.localhost else None, with_exasol=args.exasol)


if __name__ == "__main__":
    main()
