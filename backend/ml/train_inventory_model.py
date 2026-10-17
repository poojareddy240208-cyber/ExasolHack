"""
LightGBM Predictive Inventory Surge Model Training Script.
==========================================================
Generates synthetic omnichannel cart telemetry and trains a LightGBM Regressor
to forecast 15-minute demand surges (`demand_next_15m`).

Features:
- `cart_velocity_5m`: Number of active add-to-carts in rolling 5m Redis window.
- `hour_of_day`: Hour in UTC (0 - 23).
- `day_of_week`: Day index (0 = Monday, 6 = Sunday).

Target:
- `demand_next_15m`: Actual units purchased in the subsequent 15-minute window.

Output:
- Native LightGBM plain-text model saved to `backend/ml/weights/inventory_model.txt`
  ready for deployment into Exasol BucketFS (`/buckets/bfsdefault/default/inventory_model.txt`).
"""

import os
import logging
from pathlib import Path
import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.model_selection import train_test_split
from sklearn.metrics import mean_squared_error, mean_absolute_error

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("omnisync.ml.train")

# Define target paths
BASE_DIR = Path(__file__).resolve().parent
WEIGHTS_DIR = BASE_DIR / "weights"
MODEL_PATH = WEIGHTS_DIR / "inventory_model.txt"


def generate_synthetic_cart_data(n_samples: int = 10000, random_seed: int = 42) -> pd.DataFrame:
    """
    Generates a realistic synthetic dataset simulating omnichannel e-commerce traffic.
    Simulates peak evening hours and weekend shopping volume spikes.
    """
    np.random.seed(random_seed)

    # 1. Feature: Hour of day (0 to 23)
    hour_of_day = np.random.randint(0, 24, size=n_samples)

    # 2. Feature: Day of week (0=Mon, 6=Sun)
    day_of_week = np.random.randint(0, 7, size=n_samples)

    # Base traffic intensity by hour: Peak between 17:00 and 22:00
    hour_intensity = np.where((hour_of_day >= 17) & (hour_of_day <= 22), 2.2, 1.0)
    # Weekend multiplier: Higher volume on Fri (4), Sat (5), Sun (6)
    weekend_multiplier = np.where(day_of_week >= 4, 1.5, 1.0)

    # 3. Feature: Cart velocity (5m window) - driven by intensity + Poisson noise
    base_velocity = np.random.poisson(lam=12.0 * hour_intensity * weekend_multiplier)
    cart_velocity_5m = np.clip(base_velocity, 0, 150)

    # 4. Target variable: demand_next_15m
    # Realistic conversion: ~65-80% of carts convert within 15 min, with surges during peak hours
    conversion_rate = 0.72 + (0.05 * (hour_intensity - 1.0))
    expected_demand = (cart_velocity_5m * conversion_rate) + np.random.normal(0, 2.0, size=n_samples)
    demand_next_15m = np.maximum(0, np.round(expected_demand)).astype(int)

    df = pd.DataFrame(
        {
            "cart_velocity_5m": cart_velocity_5m,
            "hour_of_day": hour_of_day,
            "day_of_week": day_of_week,
            "demand_next_15m": demand_next_15m,
        }
    )
    return df


def train_and_save_inventory_model(
    output_path: Path = MODEL_PATH,
    n_samples: int = 10000,
) -> lgb.LGBMRegressor:
    """
    Trains a LightGBM Regressor on synthetic cart data and exports the text model artifact.
    """
    logger.info(f"Generating {n_samples:,} synthetic telemetry samples...")
    df = generate_synthetic_cart_data(n_samples=n_samples)

    feature_cols = ["cart_velocity_5m", "hour_of_day", "day_of_week"]
    target_col = "demand_next_15m"

    X = df[feature_cols]
    y = df[target_col]

    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)

    logger.info("Training LightGBM Regressor (surging demand predictor)...")
    model = lgb.LGBMRegressor(
        n_estimators=120,
        learning_rate=0.06,
        max_depth=5,
        num_leaves=31,
        random_state=42,
        verbosity=-1,
    )
    model.fit(X_train, y_train)

    # Evaluate model
    y_pred = model.predict(X_test)
    rmse = float(np.sqrt(mean_squared_error(y_test, y_pred)))
    mae = float(mean_absolute_error(y_test, y_pred))
    logger.info(f"Model Evaluation -> Test RMSE: {rmse:.3f} units | Test MAE: {mae:.3f} units")

    # Ensure output directory exists
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Save model in LightGBM native text format for Exasol BucketFS
    logger.info(f"Saving LightGBM model text artifact to: {output_path}")
    model.booster_.save_model(str(output_path))

    logger.info("Model saved successfully. Ready for Exasol BucketFS upload.")
    return model


if __name__ == "__main__":
    train_and_save_inventory_model()
