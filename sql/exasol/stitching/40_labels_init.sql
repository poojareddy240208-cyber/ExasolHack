-- =============================================================================
-- Identity stitching, stage 4: start connected-component labelling.
-- Every node begins as its own cluster. 45_propagate_labels.sql is then repeated by the Python runner
-- until no label changes (Exasol has no recursive queries, so the loop lives outside the SQL).
-- =============================================================================

DELETE FROM IDENTITY_LABELS;

INSERT INTO IDENTITY_LABELS (NODE_ID, CLUSTER_LABEL)
SELECT NODE_ID, NODE_ID FROM IDENTITY_NODES;
