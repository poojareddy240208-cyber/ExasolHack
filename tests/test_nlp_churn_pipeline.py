"""
Unit and Integration Tests for Module 2 Step 1:
NLP Pipeline & VIP Churn Radar.
==============================================
Validates:
1. Local HuggingFace Transformer model weights and tokenizer integrity.
2. Sentiment scoring & churn probability calculation (negative sentiment confidence).
3. Exasol NLP UDF & Golden JOIN SQL script compliance.
4. Zendesk webhook extraction, staging, and VIP churn triage logic.
5. PyExasol database execution with mock connection injection.
"""

from pathlib import Path
from unittest.mock import MagicMock
import pytest

from backend.ml.prep_nlp_model import (
    NLP_MODEL_DIR,
    load_local_nlp_model,
    predict_churn_score,
)
from backend.tasks.zendesk import process_zendesk_ticket
from backend.celery_app import process_zendesk_ticket_task


def test_nlp_model_weights_exist():
    """Validates that Transformer model weights and tokenizer files exist locally."""
    assert NLP_MODEL_DIR.exists(), f"Weights directory missing at {NLP_MODEL_DIR}"

    config_file = NLP_MODEL_DIR / "config.json"
    weights_file = NLP_MODEL_DIR / "model.safetensors"
    tokenizer_file = NLP_MODEL_DIR / "tokenizer_config.json"

    assert config_file.exists(), "config.json missing"
    assert weights_file.exists(), "model.safetensors missing"
    assert tokenizer_file.exists(), "tokenizer_config.json missing"
    assert weights_file.stat().st_size > 10_000_000, "model.safetensors appears incomplete"


def test_nlp_model_sentiment_inference():
    """
    Validates model inference accuracy:
    - High negative sentiment (severe complaint) -> Churn Score >= 0.85
    - Positive sentiment (enthusiastic customer) -> Churn Score < 0.15
    """
    angry_ticket = (
        "I am absolutely furious! My package arrived completely damaged, your support team was dismissive, "
        "and nobody is answering my calls. Cancel my subscription immediately and issue a full refund!"
    )
    score_angry = predict_churn_score(angry_ticket)
    assert score_angry >= 0.85, f"Expected churn score >= 0.85 for angry ticket, got {score_angry}"

    happy_ticket = (
        "Thank you so much! The order was delivered early in perfect condition. "
        "The customer service team was wonderfully helpful and polite."
    )
    score_happy = predict_churn_score(happy_ticket)
    assert score_happy < 0.15, f"Expected churn score < 0.15 for happy ticket, got {score_happy}"

    empty_score = predict_churn_score("")
    assert empty_score == 0.0


def test_exasol_udf_sql_script():
    """
    Validates the structure and specifications of backend/db/exasol/02_nlp_churn_udf.sql:
    - Scalar UDF definition with ticket_text parameter
    - BucketFS model path: '/buckets/bfsdefault/default/nlp_model/'
    - Golden JOIN query filtering on churn_score >= 0.85 and ltv_tier = 'Top 20%'
    """
    sql_path = Path("backend/db/exasol/02_nlp_churn_udf.sql")
    assert sql_path.exists(), f"SQL file not found at {sql_path}"

    content = sql_path.read_text(encoding="utf-8")

    # Verify UDF signature
    assert "CREATE OR REPLACE PYTHON3 SCALAR SCRIPT predict_churn" in content
    assert "ticket_text VARCHAR(20000)" in content
    assert "RETURNS DOUBLE" in content

    # Verify BucketFS path
    assert "'/buckets/bfsdefault/default/nlp_model/'" in content

    # Verify table and Golden JOIN logic
    assert "CREATE OR REPLACE TABLE support_tickets" in content
    assert "CREATE OR REPLACE TABLE customer_ltv" in content
    assert "predict_churn(t.description) >= 0.85" in content
    assert "c.ltv_tier = 'Top 20%'" in content


def test_zendesk_task_extraction_and_routing():
    """
    Validates that Zendesk ticket payloads are parsed accurately, extracting
    user_id, ticket_id, description, and classifying VIP vs non-VIP churn risk.
    """
    vip_payload = {
        "ticket_id": "TCK-9901",
        "user_id": "USR-VIP-101",
        "subject": "Horrible service - cancelling",
        "description": "Furious with broken items. Cancel my account immediately!",
        "status": "NEW",
    }

    result = process_zendesk_ticket(vip_payload)
    assert result["ticket_id"] == "TCK-9901"
    assert result["user_id"] == "USR-VIP-101"
    assert result["sentiment_score"] >= 0.85
    assert result["is_vip_churn_risk"] is True
    assert result["action_taken"] == "VIP_RETENTION_INTERVENTION_DISPATCHED"

    standard_payload = {
        "ticket_id": "TCK-9902",
        "user_id": "USR-STD-202",
        "subject": "Great service",
        "description": "I love your product, arrived super fast!",
        "status": "SOLVED",
    }

    std_result = process_zendesk_ticket(standard_payload)
    assert std_result["ticket_id"] == "TCK-9902"
    assert std_result["sentiment_score"] < 0.15
    assert std_result["is_vip_churn_risk"] is False
    assert std_result["action_taken"] == "STANDARD_ROUTING"


def test_zendesk_task_pyexasol_mock_staging():
    """
    Validates that pyexasol executes INSERT INTO support_tickets and
    the Golden JOIN query with proper parameters.
    """
    mock_conn = MagicMock()
    mock_stmt = MagicMock()
    mock_conn.execute.return_value = mock_stmt

    # Simulate VIP Golden JOIN match returned by Exasol
    mock_stmt.fetchone.side_effect = [
        ("TCK-100", "USR-VIP-1", "vip@test.com", 25000.0, "Top 20%", 0.95),  # First fetchone: VIP match
    ]

    payload = {
        "ticket_id": "TCK-100",
        "user_id": "USR-VIP-1",
        "subject": "Defective item",
        "description": "I demand an immediate cancellation and refund!",
        "status": "NEW",
    }

    result = process_zendesk_ticket(payload, connection=mock_conn)

    assert result["db_ingested"] is True
    assert result["is_vip_churn_risk"] is True
    assert result["sentiment_score"] == 0.95
    assert result["action_taken"] == "VIP_RETENTION_INTERVENTION_DISPATCHED"

    # Verify SQL calls
    assert mock_conn.execute.call_count >= 2
    first_call_sql = mock_conn.execute.call_args_list[0][0][0]
    assert "INSERT INTO support_tickets" in first_call_sql

    second_call_sql = mock_conn.execute.call_args_list[1][0][0]
    assert "predict_churn(t.description) >=" in second_call_sql
    assert "c.ltv_tier = 'Top 20%'" in second_call_sql


def test_celery_task_integration():
    """Validates Celery task entrypoint returns standardized response dictionary."""
    payload = {
        "ticket_id": "TCK-CELERY-1",
        "user_id": "USR-VIP-555",
        "description": "Order is terrible and support was rude, cancel my account!",
    }

    response = process_zendesk_ticket_task.apply(args=[payload]).get()
    assert response["ticket_id"] == "TCK-CELERY-1"
    assert response["status"] == "PROCESSED"
    assert "sentiment_score" in response
    assert "is_vip_churn_risk" in response
