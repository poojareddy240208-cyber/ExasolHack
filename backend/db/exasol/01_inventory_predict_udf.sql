-- =============================================================================
-- OmniSync Retail Analytics - Exasol In-Database Machine Learning Intelligence
-- Script 01: LightGBM 15-Minute Demand Surge Prediction & Virtual Safety Buffer UDF
-- =============================================================================
-- Architectural Role:
-- - Runs native LightGBM inference directly inside Exasol via Python3 SCALAR UDF.
-- - Model weights are read locally from BucketFS (/buckets/bfsdefault/default/inventory_model.txt).
-- - Zero data is exported out of Exasol for model evaluation.
-- - Virtual Safety Stock Buffer formula:
--     Buffer = GREATEST(0, predict_demand_15m(cart_velocity, hour, day) - current_physical_stock + cart_velocity)
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS OMNISYNC_ANALYTICS;
OPEN SCHEMA OMNISYNC_ANALYTICS;

--/
CREATE OR REPLACE PYTHON3 SCALAR SCRIPT predict_demand_15m(
    cart_velocity_5m DOUBLE, 
    hour_of_day DOUBLE, 
    day_of_week DOUBLE
) RETURNS DOUBLE AS
import os
import lightgbm as lgb
import pandas as pd
import numpy as np

# BucketFS model artifact mount point inside the Exasol container
MODEL_PATH = '/buckets/bfsdefault/default/inventory_model.txt'

# Cached booster instance across scalar executions in this container thread
_MODEL = None

def _get_model():
    global _MODEL
    if _MODEL is None:
        if not os.path.exists(MODEL_PATH):
            raise FileNotFoundError(
                f"BucketFS model weight file not found at '{MODEL_PATH}'. "
                "Ensure inventory_model.txt was uploaded to BucketFS service 'bfsdefault' bucket 'default'."
            )
        _MODEL = lgb.Booster(model_file=MODEL_PATH)
    return _MODEL

def run(ctx):
    # Handle NULL edge cases
    if ctx.cart_velocity_5m is None:
        return 0.0

    cart_vel = float(ctx.cart_velocity_5m)
    hour = float(ctx.hour_of_day) if ctx.hour_of_day is not None else 12.0
    day = float(ctx.day_of_week) if ctx.day_of_week is not None else 3.0

    # Load model from BucketFS (cached in memory)
    model = _get_model()

    # Prepare single-row DataFrame matching training feature columns
    features = pd.DataFrame([{
        'cart_velocity_5m': cart_vel,
        'hour_of_day': hour,
        'day_of_week': day
    }])

    # Execute LightGBM inference
    prediction = model.predict(features)
    
    # Return rounded non-negative demand estimate
    predicted_demand = max(0.0, float(round(prediction[0])))
    return predicted_demand
/

-- =============================================================================
-- Demonstration & Virtual Safety Stock Buffer Calculation
-- =============================================================================

-- 1. Create staging table simulating live telemetry streamed from Redis & Postgres
CREATE OR REPLACE TABLE live_sku_telemetry (
    item_id VARCHAR(64),
    current_physical_stock DECIMAL(9,0),
    cart_velocity_5m DECIMAL(9,0),
    telemetry_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Insert sample SKU traffic for demonstration
INSERT INTO live_sku_telemetry VALUES 
    ('SKU-4090-GPU', 10, 85, CURRENT_TIMESTAMP),
    ('SKU-HERMES-BAG', 3, 14, CURRENT_TIMESTAMP),
    ('SKU-AIR-JORDAN', 50, 28, CURRENT_TIMESTAMP),
    ('SKU-SMARTPHONE-PRO', 0, 42, CURRENT_TIMESTAMP);

-- 2. Query computing 15-minute predicted surge and Virtual Safety Stock Buffer
-- Formula: Buffer = GREATEST(0, predict_demand_15m(...) - current_physical_stock + cart_velocity)
SELECT 
    item_id,
    current_physical_stock,
    cart_velocity_5m,
    predict_demand_15m(
        cart_velocity_5m, 
        CAST(TO_CHAR(telemetry_timestamp, 'HH24') AS DOUBLE), 
        CAST(TO_CHAR(telemetry_timestamp, 'ID') AS DOUBLE)
    ) AS predicted_demand_15m,
    GREATEST(
        0, 
        predict_demand_15m(
            cart_velocity_5m, 
            CAST(TO_CHAR(telemetry_timestamp, 'HH24') AS DOUBLE), 
            CAST(TO_CHAR(telemetry_timestamp, 'ID') AS DOUBLE)
        ) - current_physical_stock + cart_velocity_5m
    ) AS virtual_safety_buffer
FROM live_sku_telemetry;

-- =============================================================================
-- Celery Beat Sync Command Reference:
-- EXPORT query results directly into PostgreSQL catalog ledger via JDBC
-- (Zero data routed to external middle-tier servers)
-- =============================================================================
/*
EXPORT (
    SELECT 
        item_id,
        GREATEST(
            0, 
            predict_demand_15m(
                cart_velocity_5m, 
                CAST(TO_CHAR(telemetry_timestamp, 'HH24') AS DOUBLE), 
                CAST(TO_CHAR(telemetry_timestamp, 'ID') AS DOUBLE)
            ) - current_physical_stock + cart_velocity_5m
        ) AS virtual_safety_buffer
    FROM live_sku_telemetry
) INTO JDBC 
AT postgres_jdbc_connection 
TABLE inventory_buffer_staging;
*/
