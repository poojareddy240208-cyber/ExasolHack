"""
Unit & Integration Tests for Zendesk Webhook Cleaning & Exasol Staging (Deliverable 2.1).

Verifies:
1. Text cleaning: HTML stripping, control char removal, null/empty handling, and whitespace trimming.
2. Exasol staging table creation query and PyExasol connection configuration.
3. Asynchronous Celery task processing: field extraction, cleaning, PyExasol insertion, and retry behavior.
"""

from datetime import datetime
from unittest.mock import MagicMock, patch
import pytest
import pyexasol

from backend.utils.nlp_cleaner import clean_ticket_text, normalize_timestamp
from backend.core.config import settings
from backend.core.exasol import (
    get_exasol_connection,
    init_staging_table,
    insert_staging_ticket,
    CREATE_SCHEMA_QUERY,
    CREATE_STAGING_TABLE_QUERY,
)
from backend.celery_app import process_zendesk_ticket_task


# ==============================================================================
# 1. Text Cleaner Tests
# ==============================================================================
class TestNLPCleaner:
    """Tests for backend.utils.nlp_cleaner."""

    def test_clean_html_tags_and_entities(self):
        raw_html = "<p>Hello <b>World</b>!</p><br/>Customer is &quot;unhappy&quot; &amp; wants a refund."
        cleaned = clean_ticket_text(raw_html)
        assert cleaned == 'Hello World! Customer is "unhappy" & wants a refund.'

    def test_strip_control_characters(self):
        text_with_control = "Order delivery delayed\x00\x08\x0b by 4 days.\x1f"
        cleaned = clean_ticket_text(text_with_control)
        assert cleaned == "Order delivery delayed by 4 days."

    def test_handle_null_and_empty_strings(self):
        assert clean_ticket_text(None) == ""
        assert clean_ticket_text("") == ""
        assert clean_ticket_text("    ") == ""
        assert clean_ticket_text("\n\t  \r") == ""

    def test_handle_non_string_input(self):
        assert clean_ticket_text(12345) == "12345"
        assert clean_ticket_text(99.9) == "99.9"

    def test_trim_excess_whitespace(self):
        messy_text = "   Too    many      spaces\n\n\nand   newlines.\t\t"
        cleaned = clean_ticket_text(messy_text)
        assert cleaned == "Too many spaces and newlines."

    def test_truncate_long_text_to_boundary(self):
        very_long = "A" * 5000
        cleaned = clean_ticket_text(very_long, max_length=4000)
        assert len(cleaned) == 4000
        assert cleaned == "A" * 4000

    def test_normalize_timestamp_formats(self):
        # ISO string with Z
        assert normalize_timestamp("2026-10-17T12:30:00Z") == "2026-10-17 12:30:00"
        # ISO string with offset
        assert normalize_timestamp("2026-10-17T12:30:00+00:00") == "2026-10-17 12:30:00"
        # Datetime object
        dt = datetime(2026, 10, 17, 15, 45, 30)
        assert normalize_timestamp(dt) == "2026-10-17 15:45:30"
        # None defaults to current timestamp format
        res = normalize_timestamp(None)
        assert len(res) == 19
        assert res[4] == "-" and res[7] == "-" and res[10] == " " and res[13] == ":"


# ==============================================================================
# 2. Exasol Connection & DDL Staging Table Tests
# ==============================================================================
class TestExasolStaging:
    """Tests for backend.core.exasol."""

    @patch("backend.core.exasol.pyexasol.connect")
    def test_get_exasol_connection_parameters(self, mock_connect):
        mock_conn = MagicMock()
        mock_connect.return_value = mock_conn

        conn = get_exasol_connection()
        mock_connect.assert_called_once()
        call_kwargs = mock_connect.call_args[1]
        assert call_kwargs["dsn"] == f"{settings.EXASOL_HOST}:{settings.EXASOL_PORT}"
        assert call_kwargs["user"] == settings.EXASOL_USER
        assert call_kwargs["password"] == settings.EXASOL_PASSWORD
        assert call_kwargs["autocommit"] is True
        assert conn == mock_conn

    def test_init_staging_table_executes_ddl(self):
        mock_conn = MagicMock()
        init_staging_table(conn=mock_conn)

        executed_queries = [call[0][0] for call in mock_conn.execute.call_args_list]
        assert CREATE_SCHEMA_QUERY in executed_queries
        assert CREATE_STAGING_TABLE_QUERY in executed_queries

    def test_insert_staging_ticket_executes_insert(self):
        mock_conn = MagicMock()
        insert_staging_ticket(
            ticket_id="ZD-9999",
            user_id="user_test_123",
            raw_text="<p>Raw text</p>",
            cleaned_text="Raw text",
            created_at="2026-10-17 12:00:00",
            status="PENDING",
            conn=mock_conn,
        )

        assert mock_conn.execute.called
        # Check query arguments
        last_call = mock_conn.execute.call_args
        sql = last_call[0][0]
        params = last_call[1]["query_params"]

        assert "INSERT INTO OMNISYNC_ANALYTICS.STAGING_ZENDESK_TICKETS" in sql
        assert params["ticket_id"] == "ZD-9999"
        assert params["user_id"] == "user_test_123"
        assert params["raw_text"] == "<p>Raw text</p>"
        assert params["cleaned_text"] == "Raw text"
        assert params["status"] == "PENDING"


# ==============================================================================
# 3. Celery Task Implementation & Error Handling Tests
# ==============================================================================
class TestProcessZendeskTicketTask:
    """Tests for backend.celery_app.process_zendesk_ticket_task."""

    @patch("backend.celery_app.insert_staging_ticket")
    def test_task_processes_valid_flat_payload(self, mock_insert):
        payload = {
            "ticket_id": "ZD-101",
            "user_id": "user_456",
            "subject": "Missing item in order",
            "description": "<div>Hello, I ordered 3 shirts but <b>only received 2</b>. \x00Please resolve ASAP!</div>",
            "created_at": "2026-10-17T10:00:00Z",
        }

        result = process_zendesk_ticket_task(payload)

        assert result["status"] == "STAGED"
        assert result["ticket_id"] == "ZD-101"
        assert result["user_id"] == "user_456"
        assert result["staging_table"] == "OMNISYNC_ANALYTICS.STAGING_ZENDESK_TICKETS"
        assert result["created_at"] == "2026-10-17 10:00:00"

        # Verify insert_staging_ticket received sanitized text
        mock_insert.assert_called_once()
        insert_kwargs = mock_insert.call_args[1]
        assert insert_kwargs["ticket_id"] == "ZD-101"
        assert insert_kwargs["user_id"] == "user_456"
        assert insert_kwargs["cleaned_text"] == "Hello, I ordered 3 shirts but only received 2. Please resolve ASAP!"

    @patch("backend.celery_app.insert_staging_ticket")
    def test_task_processes_nested_zendesk_structure(self, mock_insert):
        payload = {
            "ticket": {
                "id": 8842,
                "requester_id": 9931,
                "comment": {"body": "<p>I am cancelling my subscription.</p>"},
                "created_at": "2026-10-17T11:15:00Z",
            }
        }

        result = process_zendesk_ticket_task(payload)
        assert result["status"] == "STAGED"
        assert result["ticket_id"] == "8842"
        assert result["user_id"] == "9931"

        mock_insert.assert_called_once()
        insert_kwargs = mock_insert.call_args[1]
        assert insert_kwargs["cleaned_text"] == "I am cancelling my subscription."

    def test_task_raises_on_missing_ticket_id(self):
        invalid_payload = {"user_id": "user_123", "description": "No ticket id provided"}
        with pytest.raises(ValueError, match="ticket_id"):
            process_zendesk_ticket_task(invalid_payload)

    def test_task_raises_on_non_dict_payload(self):
        with pytest.raises(ValueError, match="Expected dict"):
            process_zendesk_ticket_task("not-a-dict")

    @patch("backend.celery_app.insert_staging_ticket")
    def test_task_retries_on_exasol_error(self, mock_insert):
        """Verifies Celery task triggers self.retry upon database connection/query errors."""
        mock_conn = MagicMock()
        mock_conn.options = {"verbose_error": False}
        mock_insert.side_effect = pyexasol.ExaError(mock_conn, "Mock Exasol connection dropped")

        payload = {
            "ticket_id": "ZD-ERR-01",
            "user_id": "user_retry",
            "description": "Trigger connection retry",
        }

        with patch.object(
            process_zendesk_ticket_task, "retry", side_effect=Exception("Celery Retry Raised")
        ) as mock_retry:
            with pytest.raises(Exception, match="Celery Retry Raised"):
                process_zendesk_ticket_task(payload)

            mock_retry.assert_called_once()
            retry_kwargs = mock_retry.call_args[1]
            assert retry_kwargs["countdown"] == 5
