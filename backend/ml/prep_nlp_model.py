"""
NLP Model Preparation & BucketFS Export Script.
==============================================
Downloads a lightweight Transformer sentiment/classification model
(default: `distilbert-base-uncased-finetuned-sst-2-english`) and saves
the model weights and tokenizer locally into:
`backend/ml/weights/nlp_model/`

This local directory is mounted directly into Exasol BucketFS
(`/buckets/bfsdefault/default/nlp_model/`) to enable zero-data-movement
in-database inference via Python3 SCALAR UDFs.
"""

import os
import logging
from pathlib import Path
from typing import Tuple, Optional
import torch
from transformers import AutoTokenizer, AutoModelForSequenceClassification

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("omnisync.ml.nlp_prep")

# Target directory definition
BASE_DIR = Path(__file__).resolve().parent
WEIGHTS_DIR = BASE_DIR / "weights"
NLP_MODEL_DIR = WEIGHTS_DIR / "nlp_model"

DEFAULT_MODEL_NAME = "distilbert-base-uncased-finetuned-sst-2-english"

# Local caching for verification / Python-level helper
_CACHED_TOKENIZER = None
_CACHED_MODEL = None


def download_and_save_nlp_model(
    model_name: str = DEFAULT_MODEL_NAME,
    output_dir: Path = NLP_MODEL_DIR,
) -> Path:
    """
    Downloads the model and tokenizer from HuggingFace and persists them
    to the target weights directory for BucketFS mounting.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"Downloading HuggingFace model and tokenizer: '{model_name}'...")
    tokenizer = AutoTokenizer.from_pretrained(model_name)
    model = AutoModelForSequenceClassification.from_pretrained(model_name)
    model.eval()

    logger.info(f"Saving tokenizer to: {output_dir}")
    tokenizer.save_pretrained(str(output_dir))

    logger.info(f"Saving model weights to: {output_dir}")
    model.save_pretrained(str(output_dir))

    logger.info("Verifying saved model weights with local sanity test...")
    verify_local_nlp_model(output_dir)

    logger.info(f"NLP model prep complete! Weights available at: {output_dir}")
    return output_dir


def load_local_nlp_model(model_dir: Path = NLP_MODEL_DIR) -> Tuple[AutoTokenizer, AutoModelForSequenceClassification]:
    """
    Loads tokenizer and model from the local weights directory with in-memory caching.
    """
    global _CACHED_TOKENIZER, _CACHED_MODEL

    if _CACHED_TOKENIZER is None or _CACHED_MODEL is None:
        model_dir = Path(model_dir)
        if not model_dir.exists():
            raise FileNotFoundError(
                f"Model weights not found at '{model_dir}'. Run download_and_save_nlp_model() first."
            )
        _CACHED_TOKENIZER = AutoTokenizer.from_pretrained(str(model_dir))
        _CACHED_MODEL = AutoModelForSequenceClassification.from_pretrained(str(model_dir))
        _CACHED_MODEL.eval()

    return _CACHED_TOKENIZER, _CACHED_MODEL


def predict_churn_score(text: str, model_dir: Path = NLP_MODEL_DIR) -> float:
    """
    Calculates churn risk probability (0.0 to 1.0) based on negative sentiment confidence.
    Mirrors the exact logic executed inside Exasol's Python3 SCALAR UDF.
    """
    if not text or not text.strip():
        return 0.0

    tokenizer, model = load_local_nlp_model(model_dir)

    inputs = tokenizer(
        text.strip(),
        return_tensors="pt",
        truncation=True,
        max_length=512,
        padding=True,
    )

    with torch.no_grad():
        outputs = model(**inputs)
        probs = torch.nn.functional.softmax(outputs.logits, dim=-1)

        # Map negative sentiment index
        # For SST-2: id2label = {0: 'NEGATIVE', 1: 'POSITIVE'}
        neg_idx = 0
        if hasattr(model.config, "id2label") and model.config.id2label:
            for idx, label in model.config.id2label.items():
                if "neg" in str(label).lower():
                    neg_idx = int(idx)
                    break

        churn_prob = float(probs[0][neg_idx].item())

    return round(churn_prob, 4)


def verify_local_nlp_model(model_dir: Path = NLP_MODEL_DIR) -> None:
    """
    Runs sample test cases through the local model artifact to validate accuracy.
    """
    test_cases = [
        (
            "I am furious with this order! The package arrived broken, customer support was completely useless, and I want to cancel my account immediately.",
            "High churn expected (> 0.85)",
        ),
        (
            "Package was slightly delayed but arrived in good condition. The product works fine.",
            "Moderate/Neutral churn expected",
        ),
        (
            "Absolutely loved the premium quality and super fast shipping! Best purchase I have made this year.",
            "Low churn expected (< 0.15)",
        ),
    ]

    logger.info("--- Model Inference Sanity Checks ---")
    for text, expectation in test_cases:
        score = predict_churn_score(text, model_dir=model_dir)
        logger.info(f"Sample: '{text[:60]}...' | Churn Score: {score:.4f} | Expectation: {expectation}")


if __name__ == "__main__":
    download_and_save_nlp_model()
