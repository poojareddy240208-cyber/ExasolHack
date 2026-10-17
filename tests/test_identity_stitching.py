"""
Offline tests for the Golden Record stitching pipeline (no Exasol connection needed).

They protect the architectural rules that must never regress:
- every SQL placeholder is filled, so no stage ships a literal `${...}` to Exasol,
- the label-propagation stage stays a single statement (the runner counts its updated rows),
- a promo code can never be configured strong enough to merge identities on its own.
"""

import math

import pytest

from backend.services.identity_stitching import (
    BUILD_STAGES,
    PROPAGATE_STAGE,
    PUBLISH_STAGES,
    STITCHING_PARAMS,
    load_sql,
    run_identity_stitching,
)


def test_all_stages_load_with_placeholders_filled():
    for file_name in BUILD_STAGES + [PROPAGATE_STAGE] + PUBLISH_STAGES:
        statements = load_sql(file_name, STITCHING_PARAMS)
        assert statements, f"{file_name} contains no statements"
        assert all("${" not in stmt for stmt in statements), f"{file_name} has an unfilled placeholder"


def test_propagation_stage_is_a_single_statement():
    assert len(load_sql(PROPAGATE_STAGE, STITCHING_PARAMS)) == 1


def test_stitching_stages_never_schedule_or_push():
    """Exasol stays passive: the pipeline must not contain export/scheduling commands."""
    for file_name in BUILD_STAGES + [PROPAGATE_STAGE] + PUBLISH_STAGES:
        sql = " ".join(load_sql(file_name, STITCHING_PARAMS)).upper()
        assert "EXPORT" not in sql and "JDBC" not in sql


def test_promo_alone_can_never_reach_merge_threshold():
    """Even the strongest allowed promo evidence leaves the pair below the merge threshold."""
    logit = STITCHING_PARAMS["BIAS"] + STITCHING_PARAMS["PROMO_WEIGHT_CAP"]
    probability = 1 / (1 + math.exp(-logit))
    assert probability < STITCHING_PARAMS["MERGE_THRESHOLD"]


def test_email_or_login_alone_reaches_merge_threshold():
    for weight in ("EMAIL_WEIGHT", "LINK_WEIGHT"):
        logit = STITCHING_PARAMS["BIAS"] + STITCHING_PARAMS[weight]
        assert 1 / (1 + math.exp(-logit)) >= STITCHING_PARAMS["MERGE_THRESHOLD"]


def test_runner_rejects_promo_weight_that_could_merge_alone():
    """The guard fires before any database connection is opened."""
    with pytest.raises(ValueError):
        run_identity_stitching(params={"PROMO_WEIGHT_CAP": 4.0})
