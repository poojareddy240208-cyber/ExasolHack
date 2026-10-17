-- =============================================================================
-- OmniSync Retail Analytics - Exasol In-Database Machine Learning Intelligence
-- Script 02: HuggingFace PyTorch NLP Churn Sentiment UDF & VIP Churn Radar Golden JOIN
-- =============================================================================
-- Architectural Role:
-- - Runs native Transformer inference directly inside Exasol via Python3 SCALAR UDF.
-- - HuggingFace model weights & tokenizer are read locally from BucketFS:
--     /buckets/bfsdefault/default/nlp_model/
-- - Zero ticket text or PII leaves the database for NLP model evaluation.
-- - Globalized in-memory caching ensures weights are loaded once per container thread.
-- - Golden JOIN isolates Top 20% LTV customers exhibiting high churn probability (>= 0.85).
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS OMNISYNC_ANALYTICS;
OPEN SCHEMA OMNISYNC_ANALYTICS;

--/
CREATE OR REPLACE PYTHON3 SCALAR SCRIPT predict_churn(
    ticket_text VARCHAR(20000)
) RETURNS DOUBLE AS
import os
import torch
from transformers import AutoTokenizer, AutoModelForSequenceClassification

# BucketFS model artifact mount point inside the Exasol container
MODEL_PATH = '/buckets/bfsdefault/default/nlp_model/'

# Global in-memory cache to prevent reloading model weights on each row execution
_TOKENIZER = None
_MODEL = None

def _get_model():
    """
    Retrieves or initializes the cached HuggingFace tokenizer and classification model.
    Loads directly from the local BucketFS mount.
    """
    global _TOKENIZER, _MODEL
    if _MODEL is None or _TOKENIZER is None:
        if not os.path.exists(MODEL_PATH):
            raise FileNotFoundError(
                f"BucketFS model directory not found at '{MODEL_PATH}'. "
                "Ensure NLP model weights and tokenizer were uploaded to BucketFS 'bfsdefault/default/nlp_model/'."
            )
        _TOKENIZER = AutoTokenizer.from_pretrained(MODEL_PATH)
        _MODEL = AutoModelForSequenceClassification.from_pretrained(MODEL_PATH)
        _MODEL.eval()
    return _TOKENIZER, _MODEL

def run(ctx):
    # Handle NULL or empty edge cases
    if ctx.ticket_text is None:
        return 0.0

    text = str(ctx.ticket_text).strip()
    if not text:
        return 0.0

    # Retrieve cached model and tokenizer
    tokenizer, model = _get_model()

    # Tokenize input text (truncating to maximum 512 tokens supported by model)
    inputs = tokenizer(
        text,
        return_tensors="pt",
        truncation=True,
        max_length=512,
        padding=True
    )

    with torch.no_grad():
        outputs = model(**inputs)
        # Compute normalized softmax probabilities across sentiment classes
        probs = torch.nn.functional.softmax(outputs.logits, dim=-1)

        # Dynamic negative sentiment class index lookup (SST-2: index 0 is NEGATIVE)
        neg_idx = 0
        if hasattr(model.config, "id2label") and model.config.id2label:
            for idx, label in model.config.id2label.items():
                if "neg" in str(label).lower():
                    neg_idx = int(idx)
                    break

        # Calculate churn probability based on negative sentiment confidence
        churn_prob = float(probs[0][neg_idx].item())

    # Return rounded churn probability score (0.0000 to 1.0000)
    return round(churn_prob, 4)
/

-- =============================================================================
-- Schema DDL: Support Tickets Staging & Customer Lifetime Value (LTV)
-- =============================================================================

-- 1. Ingest staging table for Zendesk ticket webhooks
CREATE OR REPLACE TABLE support_tickets (
    ticket_id VARCHAR(64) PRIMARY KEY,
    user_id VARCHAR(64),
    subject VARCHAR(255),
    description VARCHAR(20000),
    status VARCHAR(32) DEFAULT 'NEW',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 2. Customer Lifetime Value (LTV) analytical ledger
CREATE OR REPLACE TABLE customer_ltv (
    user_id VARCHAR(64) PRIMARY KEY,
    email VARCHAR(255),
    total_spend DECIMAL(12,2),
    order_count DECIMAL(9,0),
    ltv_percentile DOUBLE,
    ltv_tier VARCHAR(32), -- 'Top 20%', 'Mid Tier', 'Standard'
    calculated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- =============================================================================
-- Synthetic Data Seeding for Verification & Demonstration
-- =============================================================================

-- Seed Customer LTV profiles (VIP vs Mid vs Standard)
INSERT INTO customer_ltv VALUES
    ('USR-VIP-001', 'eleanor.vance@luxuryretail.com', 18450.00, 42, 0.98, 'Top 20%', CURRENT_TIMESTAMP),
    ('USR-VIP-002', 'marcus.sterling@enterprise.io', 12300.50, 31, 0.94, 'Top 20%', CURRENT_TIMESTAMP),
    ('USR-MID-003', 'clara.oswald@domain.com', 2150.00, 8, 0.65, 'Mid Tier', CURRENT_TIMESTAMP),
    ('USR-STD-004', 'arthur.dent@galaxy.net', 340.00, 2, 0.25, 'Standard', CURRENT_TIMESTAMP);

-- Seed Support Tickets (Angry VIP vs Happy VIP vs Frustrated Standard)
INSERT INTO support_tickets VALUES
    (
        'TCK-8801', 
        'USR-VIP-001', 
        'Unacceptable delivery failure - cancelling account!',
        'I am utterly furious with this order! My $4,000 package arrived completely smashed, customer support was rude and unhelpful, and nobody is taking responsibility. I want a full refund and I am closing my account immediately.',
        'NEW',
        CURRENT_TIMESTAMP
    ),
    (
        'TCK-8802', 
        'USR-VIP-002', 
        'Compliment on concierge experience',
        'Just wanted to thank your concierge team for the outstanding white-glove delivery yesterday. Everything arrived perfectly on time and in immaculate packaging!',
        'SOLVED',
        CURRENT_TIMESTAMP
    ),
    (
        'TCK-8803', 
        'USR-STD-004', 
        'Wrong size received',
        'I received the wrong t-shirt size in my package. Really disappointed with the mistake and want to return it.',
        'NEW',
        CURRENT_TIMESTAMP
    ),
    (
        'TCK-8804', 
        'USR-MID-003', 
        'Tracking link issue',
        'Hello, my shipping tracking link seems to be taking longer than expected to update. Could you please look into it?',
        'OPEN',
        CURRENT_TIMESTAMP
    );

-- =============================================================================
-- The "Golden JOIN" Query: VIP Churn Radar
-- =============================================================================
-- Identifies critical revenue-at-risk events by evaluating ticket sentiment
-- in-database and joining with customer lifetime value tiers.
-- Trigger condition:
--   churn_score >= 0.85 (High negative sentiment confidence)
--   AND ltv_tier = 'Top 20%' (High value customer)
-- =============================================================================

SELECT 
    t.ticket_id,
    t.user_id,
    c.email,
    c.total_spend,
    c.ltv_tier,
    predict_churn(t.description) AS churn_score,
    t.subject,
    t.description AS ticket_text,
    t.created_at
FROM support_tickets t
JOIN customer_ltv c 
    ON t.user_id = c.user_id
WHERE predict_churn(t.description) >= 0.85
  AND c.ltv_tier = 'Top 20%'
ORDER BY churn_score DESC, c.total_spend DESC;

-- =============================================================================
-- Analytical View: Real-Time VIP Churn Incident Feed
-- =============================================================================
CREATE OR REPLACE VIEW vip_churn_radar_alerts AS
SELECT 
    t.ticket_id,
    t.user_id,
    c.email,
    c.total_spend,
    c.ltv_tier,
    predict_churn(t.description) AS churn_score,
    t.subject,
    t.description AS ticket_text,
    t.created_at,
    CASE 
        WHEN predict_churn(t.description) >= 0.90 THEN 'CRITICAL_RETENTION_INTERVENTION'
        ELSE 'HIGH_PRIORITY_RETENTION'
    END AS alert_severity
FROM support_tickets t
JOIN customer_ltv c 
    ON t.user_id = c.user_id
WHERE predict_churn(t.description) >= 0.85
  AND c.ltv_tier = 'Top 20%';
