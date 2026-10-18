-- ============================================================================
-- OmniSync - Module 2: PyTorch NLP Churn Early Warning UDF
-- Registers OMNISYNC_ANALYTICS.PREDICT_CHURN_SCORE inside Exasol Personal Edition
-- Model weights mounted natively from BucketFS: /buckets/bfsdefault/models/distilbert-churn/
-- ============================================================================

-- Ensure target schema exists
CREATE SCHEMA IF NOT EXISTS OMNISYNC_ANALYTICS;
OPEN SCHEMA OMNISYNC_ANALYTICS;

--/
CREATE OR REPLACE PYTHON3 SCALAR SCRIPT OMNISYNC_ANALYTICS.PREDICT_CHURN_SCORE(cleaned_text VARCHAR(4000))
EMITS (churn_score DOUBLE) AS

import os
import sys

# ==============================================================================
# Worker-Level Model Cache
# Initialized once per Exasol worker process; persists across rows in the same transaction
# ==============================================================================
_MODEL = None
_TOKENIZER = None
_DEVICE = "cpu"
_MODEL_PATH = "/buckets/bfsdefault/models/distilbert-churn"
_FALLBACK_PATHS = [
    _MODEL_PATH,
    "/buckets/bfsdefault/default/models/distilbert-churn",
    "distilbert-base-uncased-finetuned-sst-2-english",
]


def _init_sentiment_model():
    """
    Initializes the PyTorch model and tokenizer from the BucketFS mount.
    Executes once per worker process.
    """
    global _MODEL, _TOKENIZER, _DEVICE

    if _MODEL is not None and _TOKENIZER is not None:
        return

    try:
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        _DEVICE = "cuda" if torch.cuda.is_available() else "cpu"

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
        sys.stderr.write(f"Warning: Failed to load Transformer from BucketFS ({exc})\n")


def run(ctx):
    """
    Processes each incoming row, predicting churn probability from ticket sentiment.

    SST-2 Classification Mapping:
    - Label 0: NEGATIVE
    - Label 1: POSITIVE
    Dissatisfied / negative sentiment directly correlates with customer churn intent.
    Softmax probability of NEGATIVE (label 0) is emitted as churn_score (0.0 to 1.0).
    """
    raw_val = ctx.cleaned_text

    # Guard against null, empty, or whitespace-only inputs
    if raw_val is None:
        ctx.emit(0.0)
        return

    text = str(raw_val).strip()
    if not text:
        ctx.emit(0.0)
        return

    try:
        _init_sentiment_model()

        if _MODEL is None or _TOKENIZER is None:
            # Fallback if model files are inaccessible
            ctx.emit(0.0)
            return

        import torch

        with torch.no_grad():
            inputs = _TOKENIZER(
                text,
                return_tensors="pt",
                truncation=True,
                max_length=512,
                padding=False,
            )
            inputs = {k: v.to(_DEVICE) for k, v in inputs.items()}
            outputs = _MODEL(**inputs)
            logits = outputs.logits
            probs = torch.softmax(logits, dim=-1)[0]

            # Label 0 corresponds to NEGATIVE sentiment in SST-2 fine-tuned DistilBERT
            neg_prob = float(probs[0].item())

            # Bound score between 0.0 and 1.0
            churn_score = max(0.0, min(1.0, neg_prob))
            ctx.emit(churn_score)
    except Exception as exc:
        # Invariant: Never abort the analytics query on bad row text; emit 0.0 fallback
        ctx.emit(0.0)
/
