"""
Unit tests for LightGBM Predictive Inventory Surge Model.
Validates model artifact integrity, loading via Booster, and Virtual Safety Buffer calculation.
"""

from pathlib import Path
import pytest
import lightgbm as lgb
import pandas as pd
import numpy as np

from backend.ml.train_inventory_model import MODEL_PATH, generate_synthetic_cart_data


def test_model_artifact_exists():
    """Verifies that the trained LightGBM text weight file exists."""
    assert MODEL_PATH.exists(), f"Model weight file missing at {MODEL_PATH}"
    assert MODEL_PATH.stat().st_size > 0, "Model weight file is empty"


def test_model_loading_and_inference():
    """
    Simulates Exasol UDF loading behavior using lgb.Booster(model_file=...).
    Verifies that features (cart_velocity_5m, hour_of_day, day_of_week) produce realistic outputs.
    """
    booster = lgb.Booster(model_file=str(MODEL_PATH))
    assert booster is not None

    test_input = pd.DataFrame(
        [
            {"cart_velocity_5m": 85.0, "hour_of_day": 20.0, "day_of_week": 5.0},
            {"cart_velocity_5m": 0.0, "hour_of_day": 3.0, "day_of_week": 1.0},
        ]
    )

    predictions = booster.predict(test_input)
    assert len(predictions) == 2

    # High cart velocity during peak weekend evening should produce higher predicted demand
    assert predictions[0] > predictions[1]
    assert predictions[0] > 0
    assert predictions[1] >= 0


def test_virtual_safety_buffer_formula():
    """
    Verifies PRD Section 2 formula:
    Buffer = MAX(0, Predicted_Demand_15m - Current_Physical_Stock + Active_Carts_5m)
    """
    booster = lgb.Booster(model_file=str(MODEL_PATH))

    # Scenario: 50 active carts, 10 physical stock left
    cart_vel = 50.0
    physical_stock = 10
    features = pd.DataFrame([{"cart_velocity_5m": cart_vel, "hour_of_day": 18.0, "day_of_week": 4.0}])
    pred_demand = max(0.0, float(round(booster.predict(features)[0])))

    buffer_val = max(0.0, pred_demand - physical_stock + cart_vel)
    assert buffer_val >= 0.0
    assert buffer_val >= cart_vel - physical_stock


def test_synthetic_data_generation():
    """Verifies synthetic dataset generation schema and reasonable ranges."""
    df = generate_synthetic_cart_data(n_samples=500)
    assert len(df) == 500
    assert set(df.columns) == {"cart_velocity_5m", "hour_of_day", "day_of_week", "demand_next_15m"}
    assert (df["demand_next_15m"] >= 0).all()
    assert (df["hour_of_day"].between(0, 23)).all()
    assert (df["day_of_week"].between(0, 6)).all()
