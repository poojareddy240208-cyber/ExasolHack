"""
Synthetic retail data generator for OmniSync Module 3.

Creates a population of simulated shoppers and then deliberately FRAGMENTS each one across systems,
which is the exact problem the Golden Record engine has to solve:

    one person  ->  a Shopify account            (users, purchases, returns)
                    1-3 anonymous devices         (web events, guest checkouts)
                    0-2 Zendesk requester emails  (support tickets, sometimes a different email)

Realistic noise is built in so the matching logic is tested properly:
    * Coupon-site promo codes are shared by hundreds of unrelated shoppers (promo codes must NOT merge people).
    * Some Zendesk emails are aliases (upper-case, "+support") or a completely different address.
    * Some devices never log in and never reveal an email (they cannot be linked and must stay separate).

Return behaviour follows five archetypes so all risk levels 1-5 appear in the output.

Usage (from the repository root):
    python -m scripts.generate_data --csv-only                  # write CSVs only, no database needed
    python -m scripts.generate_data --localhost --reset         # load PostgreSQL and Exasol
    python -m scripts.generate_data --persons 12000 --seed 7

A `ground_truth.csv` (node -> real person) is always written next to the CSVs so the stitching
accuracy can be measured later.
"""

import argparse
import io
import json
import logging
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
from faker import Faker

from backend.core.config import settings
from backend.services.exasol_client import connect_exasol
from backend.services.postgres_client import connect_postgres

logger = logging.getLogger("omnisync.generator")

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "data" / "generated"
EXASOL_SCHEMA = "OMNISYNC_ANALYTICS"

# (name, probability, (min, max) return rate) -> covers risk levels 1-5
ARCHETYPES = [
    ("normal", 0.55, (0.00, 0.08)),
    ("light", 0.15, (0.10, 0.24)),
    ("occasional", 0.15, (0.26, 0.39)),
    ("heavy", 0.08, (0.42, 0.58)),
    ("serial", 0.07, (0.62, 0.85)),
]

# Coupon-site codes: used by many unrelated people, so a shared code proves nothing.
GLOBAL_PROMO_CODES = ["SAVE10", "WELCOME20", "FREESHIP", "BLACKFRIDAY", "SPRING15", "VIP25"]

CATALOG = {
    "SKU-4090-GPU": 1599.00,
    "SKU-HERMES-BAG": 9200.00,
    "SKU-AIR-JORDAN": 180.00,
    "SKU-LAPTOP-PRO": 1299.00,
    "SKU-HEADPHONES": 249.00,
    "SKU-SMARTWATCH": 399.00,
    "SKU-DENIM-JACKET": 89.00,
    "SKU-RUNNING-SHOE": 120.00,
    "SKU-COFFEE-MAKER": 159.00,
    "SKU-BACKPACK": 75.00,
    "SKU-SUNGLASSES": 145.00,
    "SKU-YOGA-MAT": 49.00,
}

RETURN_REASONS = ["Wrong size", "Changed my mind", "Item not as described", "Arrived damaged", "Found cheaper"]

TICKET_SUBJECTS = [
    "Where is my order?",
    "Item arrived damaged",
    "Refund still not received",
    "Wrong size delivered",
    "Cannot log in to my account",
    "Delayed shipment again",
]

PAGES = ["/home", "/category/shoes", "/category/electronics", "/p/air-jordan", "/p/laptop-pro", "/cart", "/checkout"]
ACTIONS = ["page_view", "page_view", "search", "add_to_cart", "begin_checkout"]


class DataGenerator:
    """Builds all source tables for `persons` simulated shoppers."""

    def __init__(self, persons: int, seed: int):
        self.persons = persons
        self.rng = random.Random(seed)
        self.fake = Faker()
        Faker.seed(seed)
        self.now = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)
        self.users: List[dict] = []
        self.purchases: List[dict] = []
        self.returns: List[dict] = []
        self.tickets: List[dict] = []
        self.web_events: List[dict] = []
        self.ground_truth: List[dict] = []

    # ------------------------------------------------------------------ helpers
    def _timestamp(self, days_back_max: int, days_back_min: int = 0) -> datetime:
        """Random moment between `days_back_max` and `days_back_min` days ago."""
        seconds = self.rng.randint(days_back_min * 86400, days_back_max * 86400)
        return self.now - timedelta(seconds=seconds)

    def _pick_archetype(self) -> Tuple[str, float]:
        """Choose a return behaviour and the per-purchase return probability that goes with it."""
        roll, cumulative = self.rng.random(), 0.0
        for name, weight, (low, high) in ARCHETYPES:
            cumulative += weight
            if roll <= cumulative:
                return name, self.rng.uniform(low, high)
        return "normal", 0.03

    def _promo_code(self, personal_code: str) -> Optional[str]:
        """Mostly nothing, sometimes a shared coupon-site code, rarely a code unique to the person."""
        roll = self.rng.random()
        if roll < 0.25:
            return self.rng.choice(GLOBAL_PROMO_CODES)
        if roll < 0.30:
            return personal_code
        return None

    def _zendesk_email(self, email: str, first: str, last: str) -> str:
        """Requester email as Zendesk would see it: usually the same, sometimes messy or different."""
        roll = self.rng.random()
        if roll < 0.60:
            return email
        if roll < 0.85:
            local, domain = email.split("@")
            return self.rng.choice([f"{local.upper()}@{domain}", f"{local}+support@{domain}"])
        return f"{first[0]}{last}{self.rng.randint(1, 99)}@{self.fake.free_email_domain()}".lower()

    # ------------------------------------------------------------------ per person
    def _add_ground_truth(self, person_id: str, node_id: str) -> None:
        self.ground_truth.append({"person_id": person_id, "node_id": node_id})

    def _build_web_events(self, device_id: str, user_id: Optional[str], email: str, code: str) -> None:
        """Clickstream for one device. Only some devices ever log in or reveal their email."""
        logs_in = user_id is not None and self.rng.random() < 0.6
        reveals_email = self.rng.random() < 0.3
        for _ in range(self.rng.randint(3, 12)):
            payload = {"page": self.rng.choice(PAGES), "action": self.rng.choice(ACTIONS)}
            promo = self._promo_code(code)
            if promo:
                payload["promo"] = promo
            if reveals_email and self.rng.random() < 0.3:
                payload["email_hint"] = email  # e.g. newsletter signup box
            logged_in = logs_in and self.rng.random() < 0.5
            self.web_events.append(
                {
                    "event_ts": self._timestamp(365),
                    "device_id": device_id,
                    "session_id": f"sess_{self.rng.getrandbits(40):010x}",
                    "user_id": user_id if logged_in else None,
                    "payload": json.dumps(payload),
                }
            )

    def _build_purchases(self, buyer_ids: List[str], code: str, return_rate: float) -> None:
        """Purchases (and the returns they cause) for one person, spread over the buyer ids they used."""
        for _ in range(self.rng.randint(1, 10)):
            item_id = self.rng.choice(list(CATALOG))
            purchase_id = f"PUR-{len(self.purchases) + 1:08d}"
            purchased_at = self._timestamp(365)
            buyer = self.rng.choice(buyer_ids)
            self.purchases.append(
                {
                    "purchase_id": purchase_id,
                    "user_id": buyer,
                    "item_id": item_id,
                    "quantity": self.rng.choice([1, 1, 1, 2]),
                    "unit_price": CATALOG[item_id],
                    "promo_code": self._promo_code(code),
                    "purchased_at": purchased_at,
                }
            )
            returned_at = purchased_at + timedelta(days=self.rng.randint(3, 30), seconds=self.rng.randint(0, 86399))
            if self.rng.random() < return_rate and returned_at <= self.now:
                self.returns.append(
                    {
                        "return_id": f"RET-{len(self.returns) + 1:08d}",
                        "purchase_id": purchase_id,
                        "user_id": buyer,
                        "reason": self.rng.choice(RETURN_REASONS),
                        "refund_amount": CATALOG[item_id],
                        "returned_at": returned_at,
                    }
                )

    def _build_tickets(self, person_id: str, user_id: Optional[str], email: str, first: str, last: str) -> None:
        """0-2 Zendesk tickets; the requester may be logged in (user_id) or just an email address."""
        for _ in range(self.rng.choice([0, 0, 1, 1, 2])):
            requester = self._zendesk_email(email, first, last)
            self._add_ground_truth(person_id, f"ZENDESK:{requester.lower()}")
            self.tickets.append(
                {
                    "ticket_id": f"ZD-{100001 + len(self.tickets)}",
                    "user_id": user_id if self.rng.random() < 0.5 else None,
                    "requester_email": requester,
                    "subject": self.rng.choice(TICKET_SUBJECTS),
                    "description": self.fake.paragraph(nb_sentences=3),
                    "priority": self.rng.choice(["low", "normal", "normal", "high", "urgent"]),
                    "created_at": self._timestamp(365),
                }
            )

    def _build_person(self, index: int) -> None:
        person_id = f"P{index:06d}"
        first, last = self.fake.first_name(), self.fake.last_name()
        email = f"{first}.{last}{index}@{self.fake.free_email_domain()}".lower()
        personal_code = f"PERS{index:06d}"
        _, return_rate = self._pick_archetype()

        has_account = self.rng.random() < 0.85
        user_id = f"user_{index:06d}" if has_account else None
        devices = [f"dev_{self.rng.getrandbits(48):012x}" for _ in range(self.rng.randint(1, 3))]

        if user_id:
            self.users.append(
                {
                    "user_id": user_id,
                    "shopify_customer_id": f"SHP-{index:07d}",
                    "email": email,
                    "full_name": f"{first} {last}",
                    "phone": self.fake.numerify("+1-###-###-####"),
                    "created_at": self._timestamp(700, 30),
                }
            )
            self._add_ground_truth(person_id, f"SHOPIFY:{user_id}")
        for device_id in devices:
            self._add_ground_truth(person_id, f"DEVICE:{device_id}")
            self._build_web_events(device_id, user_id, email, personal_code)

        # Registered shoppers mostly buy as themselves; the rest checks out as a guest on a device.
        buyers = [user_id] * 4 + devices if user_id else devices
        self._build_purchases(buyers, personal_code, return_rate)
        self._build_tickets(person_id, user_id, email, first, last)

    # ------------------------------------------------------------------ output
    def run(self) -> Dict[str, pd.DataFrame]:
        for index in range(1, self.persons + 1):
            self._build_person(index)
        frames = {
            "users": pd.DataFrame(self.users),
            "purchases": pd.DataFrame(self.purchases),
            "returns": pd.DataFrame(self.returns),
            "support_tickets": pd.DataFrame(self.tickets),
            "web_events": pd.DataFrame(self.web_events),
            "ground_truth": pd.DataFrame(self.ground_truth),
        }
        for name, frame in frames.items():
            logger.info("generated %-16s %7d rows", name, len(frame))
        return frames


# ---------------------------------------------------------------------- loading
POSTGRES_TABLES = ["users", "purchases", "returns", "support_tickets"]  # parents before children
EXASOL_TABLES = {
    "users": "RAW_USERS",
    "purchases": "RAW_PURCHASES",
    "returns": "RAW_RETURNS",
    "support_tickets": "RAW_SUPPORT_TICKETS",
    "web_events": "RAW_WEB_EVENTS",
}


DERIVED_EXASOL_TABLES = [
    "IDENTITY_NODES",
    "IDENTITY_NODE_PROMOS",
    "IDENTITY_LINKS",
    "IDENTITY_PROMO_PAIRS",
    "IDENTITY_PAIR_SCORES",
    "IDENTITY_LABELS",
    "GOLDEN_CUSTOMERS",
    "GOLDEN_CUSTOMER_MEMBERS",
    "USER_RETURN_RISK",
]


def write_csvs(frames: Dict[str, pd.DataFrame]) -> None:
    """Write every table to data/generated/<name>.csv for inspection and offline use."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, frame in frames.items():
        frame.to_csv(OUTPUT_DIR / f"{name}.csv", index=False)
    logger.info("CSV files written to %s", OUTPUT_DIR)


def load_postgres(frames: Dict[str, pd.DataFrame], host: str, reset: bool) -> None:
    """Bulk-load the transactional tables with COPY (fast, and one transaction for all of them)."""
    conn = connect_postgres(host)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET TIME ZONE 'UTC'")
            if reset:
                cur.execute("TRUNCATE users, purchases, returns, support_tickets")
            for table in POSTGRES_TABLES:
                buffer = io.StringIO()
                frames[table].to_csv(buffer, index=False, header=False)
                buffer.seek(0)
                columns = ", ".join(frames[table].columns)
                cur.copy_expert(f"COPY {table} ({columns}) FROM STDIN WITH (FORMAT csv)", buffer)
                logger.info("PostgreSQL: loaded %-16s %7d rows", table, len(frames[table]))
    finally:
        conn.close()


def load_exasol(frames: Dict[str, pd.DataFrame], host: str, reset: bool) -> None:
    """Bulk-load the raw tables into Exasol (including the unstructured JSON web events)."""
    conn = connect_exasol(host=host, schema=EXASOL_SCHEMA)
    try:
        for name, table in EXASOL_TABLES.items():
            if reset:
                conn.execute(f"TRUNCATE TABLE {table}")
            frame = frames[name].astype(object).where(frames[name].notna(), None)
            conn.import_from_pandas(frame, (EXASOL_SCHEMA, table), import_params={"columns": list(frame.columns)})
            logger.info("Exasol: loaded %-20s %7d rows", table, len(frame))
        # Derived Golden Record tables are rebuilt by the stitching job, so clear stale results.
        if reset:
            for table in DERIVED_EXASOL_TABLES:
                conn.execute(f"TRUNCATE TABLE {table}")
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate and load OmniSync synthetic retail data.")
    parser.add_argument("--persons", type=int, default=8000, help="simulated shoppers (8000 gives roughly 100k+ rows)")
    parser.add_argument("--seed", type=int, default=42, help="random seed, same seed gives the same data")
    parser.add_argument("--csv-only", action="store_true", help="write CSV files and skip both databases")
    parser.add_argument("--localhost", action="store_true", help="connect to localhost instead of docker hostnames")
    parser.add_argument("--reset", action="store_true", help="empty the target tables before loading")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    frames = DataGenerator(args.persons, args.seed).run()
    write_csvs(frames)

    if not args.csv_only:
        load_postgres(frames, "localhost" if args.localhost else settings.POSTGRES_HOST, args.reset)
        load_exasol(frames, "localhost" if args.localhost else settings.EXASOL_HOST, args.reset)
    logger.info("Done.")


if __name__ == "__main__":
    main()
