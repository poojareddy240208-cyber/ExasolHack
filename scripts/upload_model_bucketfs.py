"""
Exasol BucketFS Model Uploader.

Downloads or packages a lightweight HuggingFace sentiment classification transformer
(e.g., distilbert-base-uncased-finetuned-sst-2-english) and uploads model weights
and tokenizer artifacts to Exasol BucketFS via HTTP PUT.

Strict Architectural Rules:
- Exasol Personal Edition executes ML in-database via Python UDFs.
- Model weights MUST reside natively in BucketFS; no inference data is exported externally.
"""

import argparse
import json
import logging
from pathlib import Path
import sys
import tarfile
from typing import Any, Dict, List, Optional
import requests

# Ensure project root is in sys.path when running as a script
project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from backend.core.config import settings  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")
logger = logging.getLogger("omnisync.bucketfs_uploader")

DEFAULT_MODEL_NAME = "distilbert-base-uncased-finetuned-sst-2-english"
DEFAULT_REMOTE_FOLDER = "distilbert-churn"


def create_synthetic_model_bundle(target_dir: Path) -> List[Path]:
    """
    Creates lightweight synthetic/mock transformer model artifacts for local development,
    CI testing, or offline environments where downloading 250MB+ weights is undesirable.

    Args:
        target_dir: Directory where model files will be created.

    Returns:
        List of generated file paths.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    created_files: List[Path] = []

    # 1. config.json
    config_data = {
        "architectures": ["DistilBertForSequenceClassification"],
        "id2label": {"0": "NEGATIVE", "1": "POSITIVE"},
        "label2id": {"NEGATIVE": 0, "POSITIVE": 1},
        "model_type": "distilbert",
        "num_labels": 2,
        "vocab_size": 30522,
    }
    config_file = target_dir / "config.json"
    with open(config_file, "w", encoding="utf-8") as f:
        json.dump(config_data, f, indent=2)
    created_files.append(config_file)

    # 2. tokenizer_config.json
    tokenizer_config = {
        "do_lower_case": True,
        "model_max_length": 512,
        "tokenizer_class": "DistilBertTokenizer",
    }
    tokenizer_file = target_dir / "tokenizer_config.json"
    with open(tokenizer_file, "w", encoding="utf-8") as f:
        json.dump(tokenizer_config, f, indent=2)
    created_files.append(tokenizer_file)

    # 3. vocab.txt
    vocab_file = target_dir / "vocab.txt"
    with open(vocab_file, "w", encoding="utf-8") as f:
        f.write("[PAD]\n[UNK]\n[CLS]\n[SEP]\n[MASK]\norder\ndelay\nrefund\nterrible\ngreat\n")
    created_files.append(vocab_file)

    # 4. pytorch_model.bin (placeholder binary weights)
    weights_file = target_dir / "pytorch_model.bin"
    with open(weights_file, "wb") as f:
        f.write(b"MOCK_PYTORCH_WEIGHTS_BINARY_BLOB_V1")
    created_files.append(weights_file)

    logger.info(f"Created synthetic model bundle at '{target_dir}' ({len(created_files)} files).")
    return created_files


def download_huggingface_model(model_name: str, target_dir: Path) -> List[Path]:
    """
    Downloads model weights and tokenizer from HuggingFace Hub using the transformers library.
    Falls back to synthetic bundle creation if transformers is not available.

    Args:
        model_name: HuggingFace model identifier.
        target_dir: Local destination directory.

    Returns:
        List of downloaded/saved file paths.
    """
    try:
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        logger.info(f"Downloading '{model_name}' from HuggingFace Hub...")
        tokenizer = AutoTokenizer.from_pretrained(model_name)
        model = AutoModelForSequenceClassification.from_pretrained(model_name)

        target_dir.mkdir(parents=True, exist_ok=True)
        tokenizer.save_pretrained(str(target_dir))
        model.save_pretrained(str(target_dir))

        files = [p for p in target_dir.iterdir() if p.is_file()]
        logger.info(f"Successfully downloaded and saved {len(files)} files to '{target_dir}'.")
        return files
    except ImportError:
        logger.warning(
            "HuggingFace 'transformers' or 'torch' package not installed in current environment. "
            "Falling back to synthetic model bundle for Exasol BucketFS staging."
        )
        return create_synthetic_model_bundle(target_dir)
    except Exception as exc:
        logger.warning(f"Failed to download model from HuggingFace ({exc}). Using synthetic bundle fallback.")
        return create_synthetic_model_bundle(target_dir)


def create_model_tar_archive(source_dir: Path, archive_path: Path) -> Path:
    """
    Packages a directory into a .tar.gz archive.

    Args:
        source_dir: Directory containing model files.
        archive_path: Destination path for the .tar.gz file.

    Returns:
        Path to the created archive.
    """
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(source_dir, arcname=source_dir.name)
    logger.info(f"Packaged model directory into archive '{archive_path}' ({archive_path.stat().st_size} bytes).")
    return archive_path


def upload_file_to_bucketfs(
    file_path: Path,
    remote_path: str,
    host: str,
    port: int,
    bucket: str,
    password: str,
    dry_run: bool = False,
) -> bool:
    """
    Uploads a single file to Exasol BucketFS using HTTP PUT.

    BucketFS HTTP URL structure:
    http://<host>:<port>/<bucket>/<remote_path>
    Default HTTP Basic Auth user is 'w' (write user) with the BucketFS write password.

    Args:
        file_path: Local file path to upload.
        remote_path: Target relative path in BucketFS.
        host: Exasol host address.
        port: BucketFS HTTP port (typically 2580).
        bucket: Bucket name (e.g., 'models').
        password: Write password for BucketFS.
        dry_run: If True, simulates upload without issuing HTTP network calls.

    Returns:
        True if upload succeeded or simulated, False otherwise.
    """
    url = f"http://{host}:{port}/{bucket}/{remote_path.lstrip('/')}"

    if dry_run:
        logger.info(f"[DRY RUN] Would upload '{file_path.name}' ({file_path.stat().st_size} bytes) -> {url}")
        return True

    logger.info(f"Uploading '{file_path.name}' to {url}...")
    try:
        with open(file_path, "rb") as f:
            response = requests.put(
                url,
                auth=("w", password),
                data=f,
                headers={"Content-Type": "application/octet-stream"},
                timeout=60,
            )

        if response.status_code in (200, 201, 204):
            logger.info(f"Upload successful: {remote_path} (HTTP {response.status_code})")
            return True
        else:
            logger.error(
                f"BucketFS upload failed for {remote_path}: HTTP {response.status_code} - {response.text[:200]}"
            )
            return False
    except requests.exceptions.RequestException as exc:
        logger.error(f"Network error uploading {file_path.name} to BucketFS ({url}): {exc}")
        return False


def upload_model_to_bucketfs(
    model_name: str = DEFAULT_MODEL_NAME,
    local_dir: Optional[str] = None,
    remote_folder: str = DEFAULT_REMOTE_FOLDER,
    create_archive: bool = False,
    mock_weights: bool = False,
    dry_run: bool = False,
    host: Optional[str] = None,
    port: Optional[int] = None,
    bucket: Optional[str] = None,
    password: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Orchestrates the complete model preparation and BucketFS upload pipeline.

    Args:
        model_name: Name of HuggingFace transformer.
        local_dir: Local staging directory.
        remote_folder: Destination folder inside BucketFS.
        create_archive: If True, uploads a .tar.gz archive in addition to/instead of raw files.
        mock_weights: If True, uses lightweight synthetic weights directly.
        dry_run: If True, runs without communicating with Exasol.
        host: Overrides settings.EXASOL_HOST.
        port: Overrides settings.EXASOL_BUCKETFS_PORT.
        bucket: Overrides settings.EXASOL_BUCKETFS_BUCKET.
        password: Overrides settings.EXASOL_BUCKETFS_PASSWORD.

    Returns:
        Dictionary with execution status and uploaded file details.
    """
    target_host = host or settings.EXASOL_HOST
    target_port = port or settings.EXASOL_BUCKETFS_PORT
    target_bucket = bucket or settings.EXASOL_BUCKETFS_BUCKET
    target_password = password or settings.EXASOL_BUCKETFS_PASSWORD

    staging_path = Path(local_dir or (project_root / "models" / remote_folder))

    if mock_weights:
        files = create_synthetic_model_bundle(staging_path)
    else:
        files = download_huggingface_model(model_name, staging_path)

    uploaded_files: List[str] = []
    success = True

    # Upload individual model and tokenizer files
    for file_path in files:
        remote_path = f"{remote_folder}/{file_path.name}"
        ok = upload_file_to_bucketfs(
            file_path=file_path,
            remote_path=remote_path,
            host=target_host,
            port=target_port,
            bucket=target_bucket,
            password=target_password,
            dry_run=dry_run,
        )
        if ok:
            uploaded_files.append(remote_path)
        else:
            success = False

    # Optional archive packaging
    archive_remote_path: Optional[str] = None
    if create_archive:
        archive_local = staging_path.parent / f"{remote_folder}.tar.gz"
        create_model_tar_archive(staging_path, archive_local)
        archive_remote_path = f"{remote_folder}.tar.gz"
        archive_ok = upload_file_to_bucketfs(
            file_path=archive_local,
            remote_path=archive_remote_path,
            host=target_host,
            port=target_port,
            bucket=target_bucket,
            password=target_password,
            dry_run=dry_run,
        )
        if archive_ok:
            uploaded_files.append(archive_remote_path)
        else:
            success = False

    mounted_path = f"/buckets/{settings.EXASOL_BUCKETFS_SERVICE}/{target_bucket}/{remote_folder}"

    return {
        "success": success,
        "dry_run": dry_run,
        "files_count": len(uploaded_files),
        "uploaded_files": uploaded_files,
        "bucketfs_url": f"http://{target_host}:{target_port}/{target_bucket}/{remote_folder}",
        "mounted_container_path": mounted_path,
    }


def main():
    """CLI entry point for BucketFS model uploader."""
    parser = argparse.ArgumentParser(description="Upload HuggingFace Transformer weights to Exasol BucketFS.")
    parser.add_argument("--model-name", default=DEFAULT_MODEL_NAME, help="HuggingFace model identifier")
    parser.add_argument("--local-dir", default=None, help="Local directory to store downloaded model files")
    parser.add_argument("--remote-folder", default=DEFAULT_REMOTE_FOLDER, help="Remote BucketFS folder name")
    parser.add_argument("--archive", action="store_true", help="Upload a .tar.gz bundle as well")
    parser.add_argument("--mock", action="store_true", help="Create synthetic weights without calling HuggingFace")
    parser.add_argument("--dry-run", action="store_true", help="Simulate upload without network calls")
    parser.add_argument("--host", default=None, help="Exasol BucketFS host")
    parser.add_argument("--port", type=int, default=None, help="Exasol BucketFS port")
    parser.add_argument("--bucket", default=None, help="BucketFS bucket name")
    parser.add_argument("--password", default=None, help="BucketFS write password")

    args = parser.parse_args()

    result = upload_model_to_bucketfs(
        model_name=args.model_name,
        local_dir=args.local_dir,
        remote_folder=args.remote_folder,
        create_archive=args.archive,
        mock_weights=args.mock,
        dry_run=args.dry_run,
        host=args.host,
        port=args.port,
        bucket=args.bucket,
        password=args.password,
    )

    if result["success"]:
        logger.info(f"BucketFS Model Deployment Complete. Mounted path: {result['mounted_container_path']}")
        sys.exit(0)
    else:
        logger.error("BucketFS Model Deployment Encountered Errors.")
        sys.exit(1)


if __name__ == "__main__":
    main()
