"""
Unit & Integration Tests for Exasol BucketFS Model Upload & PyTorch Churn UDF (Deliverable 2.2).

Verifies:
1. BucketFS upload utility: packaging, synthetic bundle creation, tar archiving, and HTTP PUT upload logic (mocked).
2. Exasol PyTorch Churn UDF SQL generation: scalar script signature, worker cache, fallback paths,
   and score calculation.
3. Integration helper `register_churn_udf` executing on mock PyExasol connection.
4. Offline churn scoring logic and threshold semantics.
"""

from unittest.mock import MagicMock, patch
import requests

from backend.core.config import settings
from backend.core.exasol import register_churn_udf, CREATE_SCHEMA_QUERY
from backend.core.exasol_udf import (
    get_churn_udf_sql,
    predict_churn_score_offline,
)
from scripts.upload_model_bucketfs import (
    create_synthetic_model_bundle,
    create_model_tar_archive,
    upload_file_to_bucketfs,
    upload_model_to_bucketfs,
)


# ==============================================================================
# 1. BucketFS Upload Script Tests
# ==============================================================================
class TestBucketFSUploader:
    """Tests for scripts/upload_model_bucketfs.py."""

    def test_create_synthetic_model_bundle(self, tmp_path):
        target_dir = tmp_path / "test_model"
        files = create_synthetic_model_bundle(target_dir)

        assert len(files) == 4
        file_names = {f.name for f in files}
        assert "config.json" in file_names
        assert "tokenizer_config.json" in file_names
        assert "vocab.txt" in file_names
        assert "pytorch_model.bin" in file_names

    def test_create_model_tar_archive(self, tmp_path):
        target_dir = tmp_path / "model_to_archive"
        create_synthetic_model_bundle(target_dir)

        archive_path = tmp_path / "model_bundle.tar.gz"
        created_archive = create_model_tar_archive(target_dir, archive_path)

        assert created_archive.exists()
        assert created_archive.stat().st_size > 0

    @patch("scripts.upload_model_bucketfs.requests.put")
    def test_upload_file_to_bucketfs_success(self, mock_put, tmp_path):
        test_file = tmp_path / "model.bin"
        test_file.write_bytes(b"test_weight_data")

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_put.return_value = mock_response

        ok = upload_file_to_bucketfs(
            file_path=test_file,
            remote_path="distilbert-churn/model.bin",
            host="127.0.0.1",
            port=2580,
            bucket="models",
            password="write_secret",
            dry_run=False,
        )

        assert ok is True
        mock_put.assert_called_once()
        call_url = mock_put.call_args[0][0]
        call_kwargs = mock_put.call_args[1]

        assert call_url == "http://127.0.0.1:2580/models/distilbert-churn/model.bin"
        assert call_kwargs["auth"] == ("w", "write_secret")
        assert call_kwargs["headers"] == {"Content-Type": "application/octet-stream"}

    @patch("scripts.upload_model_bucketfs.requests.put")
    def test_upload_file_to_bucketfs_http_error(self, mock_put, tmp_path):
        test_file = tmp_path / "model.bin"
        test_file.write_bytes(b"test_weight_data")

        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_response.text = "Forbidden"
        mock_put.return_value = mock_response

        ok = upload_file_to_bucketfs(
            file_path=test_file,
            remote_path="distilbert-churn/model.bin",
            host="127.0.0.1",
            port=2580,
            bucket="models",
            password="wrong_password",
            dry_run=False,
        )

        assert ok is False

    @patch("scripts.upload_model_bucketfs.requests.put")
    def test_upload_file_to_bucketfs_network_exception(self, mock_put, tmp_path):
        test_file = tmp_path / "model.bin"
        test_file.write_bytes(b"test_weight_data")

        mock_put.side_effect = requests.exceptions.ConnectionError("Connection refused")

        ok = upload_file_to_bucketfs(
            file_path=test_file,
            remote_path="distilbert-churn/model.bin",
            host="127.0.0.1",
            port=2580,
            bucket="models",
            password="secret",
            dry_run=False,
        )

        assert ok is False

    def test_upload_file_to_bucketfs_dry_run(self, tmp_path):
        test_file = tmp_path / "model.bin"
        test_file.write_bytes(b"dry_run_data")

        with patch("scripts.upload_model_bucketfs.requests.put") as mock_put:
            ok = upload_file_to_bucketfs(
                file_path=test_file,
                remote_path="distilbert-churn/model.bin",
                host="127.0.0.1",
                port=2580,
                bucket="models",
                password="secret",
                dry_run=True,
            )
            assert ok is True
            mock_put.assert_not_called()

    @patch("scripts.upload_model_bucketfs.requests.put")
    def test_upload_model_to_bucketfs_orchestration(self, mock_put, tmp_path):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_put.return_value = mock_response

        local_dir = str(tmp_path / "models_pipeline")

        result = upload_model_to_bucketfs(
            local_dir=local_dir,
            remote_folder="test-churn",
            create_archive=True,
            mock_weights=True,
            dry_run=False,
            host="exasol",
            port=2580,
            bucket="models",
            password="secret",
        )

        assert result["success"] is True
        assert result["files_count"] == 5  # 4 model files + 1 archive
        assert result["mounted_container_path"] == f"/buckets/{settings.EXASOL_BUCKETFS_SERVICE}/models/test-churn"


# ==============================================================================
# 2. Exasol UDF SQL Generation & Architecture Tests
# ==============================================================================
class TestExasolChurnUDF:
    """Tests for backend/core/exasol_udf.py and UDF registration in backend/core/exasol.py."""

    def test_get_churn_udf_sql_contains_required_signature(self):
        sql = get_churn_udf_sql()

        # Must be scalar Python3 script with specific signature from PRD
        assert (
            "CREATE OR REPLACE PYTHON3 SCALAR SCRIPT OMNISYNC_ANALYTICS.PREDICT_CHURN_SCORE(cleaned_text VARCHAR(4000))"
            in sql
        )
        assert "RETURNS DOUBLE AS" in sql  # a scalar function: callable in a SELECT list with other columns

    def test_get_churn_udf_sql_uses_global_worker_cache(self):
        sql = get_churn_udf_sql()

        # Must declare and use global model and tokenizer cache
        assert "global _MODEL, _TOKENIZER, _DEVICE" in sql
        assert "_init_sentiment_model()" in sql

    def test_get_churn_udf_sql_has_bucketfs_path(self):
        custom_path = "/buckets/bfsdefault/default/models/distilbert-churn"
        sql = get_churn_udf_sql(bucketfs_model_path=custom_path)

        assert custom_path in sql

    def test_get_churn_udf_sql_fallback_on_null_and_error(self):
        sql = get_churn_udf_sql()

        # Must safely return 0.0 for empty text or errors
        assert "return 0.0" in sql
        assert "ctx.emit" not in sql

    def test_register_churn_udf_executes_on_connection(self):
        mock_conn = MagicMock()

        register_churn_udf(conn=mock_conn)

        executed_statements = [call[0][0] for call in mock_conn.execute.call_args_list]
        assert CREATE_SCHEMA_QUERY in executed_statements

        udf_statement = executed_statements[1]
        assert "CREATE OR REPLACE PYTHON3 SCALAR SCRIPT OMNISYNC_ANALYTICS.PREDICT_CHURN_SCORE" in udf_statement
        assert "RETURNS DOUBLE AS" in udf_statement
        assert "EMITS" not in udf_statement

    def test_predict_churn_score_offline(self):
        # Empty / whitespace inputs return 0.0
        assert predict_churn_score_offline(None) == 0.0
        assert predict_churn_score_offline("") == 0.0
        assert predict_churn_score_offline("   ") == 0.0

        # Highly negative complaint text signals high churn score
        negative_text = "I want to cancel my account immediately. Terrible delayed service and broken item. Refund now!"
        high_score = predict_churn_score_offline(negative_text)
        assert high_score >= 0.85, f"Expected churn score >= 0.85, got {high_score}"

        # Highly positive feedback text signals low churn score
        positive_text = "Great product! Excellent customer service and very happy with quick delivery."
        low_score = predict_churn_score_offline(positive_text)
        assert low_score < 0.20, f"Expected low churn score, got {low_score}"
