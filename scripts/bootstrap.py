import logging
from sqlalchemy.orm import Session
from backend.db.session import engine, SessionLocal
from backend.models.base import Base

# Import all models to ensure they are registered with SQLAlchemy's metadata
from backend.models.inventory import Inventory
from backend.models.sales import SalesLedger, RetentionCampaignAudit, UserRiskCache

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def init_db():
    logger.info("Creating initial PostgreSQL tables...")
    Base.metadata.create_all(bind=engine)
    logger.info("Tables created successfully.")

    # Seed initial inventory for the Hackathon Demo
    db: Session = SessionLocal()
    if db.query(Inventory).count() == 0:
        logger.info("Seeding initial inventory items...")
        seed_items = [
            Inventory(item_id="SKU-4090-GPU", name="RTX 4090", stock=50, version=1, virtual_safety_buffer=0),
            Inventory(item_id="SKU-HERMES-BAG", name="Hermes Birkin", stock=5, version=1, virtual_safety_buffer=0),
            Inventory(item_id="SKU-AIR-JORDAN", name="Air Jordan 1", stock=150, version=1, virtual_safety_buffer=0)
        ]
        db.add_all(seed_items)
        db.commit()
        logger.info("Seed data inserted.")
    db.close()

if __name__ == "__main__":
    init_db()