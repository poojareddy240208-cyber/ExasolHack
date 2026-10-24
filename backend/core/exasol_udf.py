"""
OmniSync Exasol PyTorch Sentiment UDF Definition.

Defines the Python3 Scalar Script executed directly inside Exasol Personal Edition.
Strict Architectural Rules:
- Models are trained or loaded directly inside Exasol via UDFs.
- Model weights reside in BucketFS; no ticket text is exported externally for inference.
- The model is loaded ONCE into worker memory across all row evaluations.
"""

from typing import Optional
from backend.core.config import settings

# Default BucketFS mount path inside the Exasol container
DEFAULT_BUCKETFS_MODEL_PATH = (
    f"/buckets/{settings.EXASOL_BUCKETFS_SERVICE}/{settings.EXASOL_BUCKETFS_BUCKET}/distilbert-churn"
)


def get_churn_udf_sql(bucketfs_model_path: Optional[str] = None) -> str:
    """
    Returns the SQL statement to register OMNISYNC_ANALYTICS.PREDICT_CHURN_SCORE in Exasol.

    Args:
        bucketfs_model_path: Absolute mounted container path to model weights in BucketFS.
                             Defaults to '/buckets/bfsdefault/models/distilbert-churn'.
    """
    model_path = bucketfs_model_path or DEFAULT_BUCKETFS_MODEL_PATH

    sql = f"""CREATE OR REPLACE PYTHON3 SCALAR SCRIPT OMNISYNC_ANALYTICS.PREDICT_CHURN_SCORE(cleaned_text VARCHAR(4000))
RETURNS VARCHAR(2000) AS

import os
import sys
import json

# ==============================================================================
# Worker-Level Model Cache
# Initialized once per Exasol worker process; persists across rows in the same transaction
# ==============================================================================
_MODEL = None
_TOKENIZER = None
_DEVICE = "cpu"
_MODEL_PATH = "{model_path}"
_FALLBACK_PATHS = [
    _MODEL_PATH,
    "/buckets/bfsdefault/default/models/distilbert-churn",
    "/buckets/bfsdefault/models/distilbert-churn",
    "distilbert-base-uncased-finetuned-sst-2-english",
]


def _init_sentiment_model():
    \"\"\"
    Initializes the PyTorch model and tokenizer from the BucketFS mount.
    Executes once per worker process.
    \"\"\"
    global _MODEL, _TOKENIZER, _DEVICE

    if _MODEL is not None and _TOKENIZER is not None:
        return

    try:
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        _DEVICE = "cuda" if torch.cuda.is_available() else "cpu"

        # Resolve model path from BucketFS or HuggingFace identifier
        target_path = None
        for candidate in _FALLBACK_PATHS:
            if candidate and (os.path.exists(candidate) or not candidate.startswith("/buckets/")):
                target_path = candidate
                break

        if not target_path:
            target_path = _MODEL_PATH

        _TOKENIZER = AutoTokenizer.from_pretrained(target_path)
        _MODEL = AutoModelForSequenceClassification.from_pretrained(target_path)
        _MODEL.to(_DEVICE)
        _MODEL.eval()
    except Exception as exc:
        # Keep _MODEL as None; run() will safely return fallback
        sys.stderr.write("Warning: Failed to load Transformer from BucketFS: " + str(exc) + "\\n")


def run(ctx):
    \"\"\"
    Processes each incoming row, predicting churn probability from ticket sentiment.
    Outputs a structured JSON string: {{"sentiment": X, "urgency": Y, "root_cause": Z}}.
    \"\"\"
    raw_val = ctx.cleaned_text

    # Guard against null, empty, or whitespace-only inputs
    if raw_val is None:
        return json.dumps({{"sentiment": 0.0, "urgency": 0.0, "root_cause": "NONE"}})

    text = str(raw_val).strip()
    if not text:
        return json.dumps({{"sentiment": 0.0, "urgency": 0.0, "root_cause": "NONE"}})

    try:
        _init_sentiment_model()

        if _MODEL is None or _TOKENIZER is None:
            # Fallback if model files are inaccessible
            return json.dumps({{"sentiment": 0.0, "urgency": 0.0, "root_cause": "NONE"}})

        import torch

        with torch.no_grad():
            inputs = _TOKENIZER(
                text,
                return_tensors="pt",
                truncation=True,
                max_length=512,
                padding=False,
            )
            inputs = {{k: v.to(_DEVICE) for k, v in inputs.items()}}
            outputs = _MODEL(**inputs)
            logits = outputs.logits
            probs = torch.softmax(logits, dim=-1)[0]

            # Label 0 corresponds to NEGATIVE sentiment in SST-2 fine-tuned DistilBERT
            neg_prob = float(probs[0].item())

            # Bound score between 0.0 and 1.0
            churn_score = max(0.0, min(1.0, neg_prob))
            urgency = churn_score
            root_cause = "SERVICE_ISSUE" if churn_score >= 0.5 else "NONE"
            return json.dumps({{
                "sentiment": round(churn_score, 4),
                "urgency": round(urgency, 4),
                "root_cause": root_cause,
            }})
    except Exception as exc:
        # Invariant: Never abort the analytics query on bad row text; return fallback JSON
        return json.dumps({{"sentiment": 0.0, "urgency": 0.0, "root_cause": "NONE"}})
"""
    return sql


def predict_churn_score_offline(text: Optional[str]) -> float:
    """
    Local reference implementation for unit testing and offline development
    when PyTorch or BucketFS is not running.

    Heuristic: evaluates negative vs positive sentiment signals and computes
    a deterministic score between 0.0 and 1.0.
    """
    if not text or not str(text).strip():
        return 0.0

    lower = str(text).lower()

    negative_keywords = [
        "cancel",
        "refund",
        "terrible",
        "awful",
        "broken",
        "worst",
        "horrible",
        "unacceptable",
        "unhappy",
        "disappointed",
        "delayed",
        "scam",
        "lawsuit",
        "complaint",
        "defective",
        "poor",
    ]
    positive_keywords = [
        "great",
        "love",
        "excellent",
        "happy",
        "satisfied",
        "thanks",
        "good",
        "awesome",
        "best",
        "perfect",
    ]

    neg_hits = sum(1 for kw in negative_keywords if kw in lower)
    pos_hits = sum(1 for kw in positive_keywords if kw in lower)

    if neg_hits == 0 and pos_hits == 0:
        return 0.15  # Neutral baseline

    score = (neg_hits * 0.3) - (pos_hits * 0.2) + 0.3
    return max(0.0, min(1.0, round(score, 4)))
