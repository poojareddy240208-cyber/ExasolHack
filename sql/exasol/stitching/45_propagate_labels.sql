-- =============================================================================
-- Identity stitching, stage 4b (repeated): one round of label propagation.
-- Each node adopts the smallest label among its merged neighbours. When a round updates 0 rows,
-- every connected group of merged nodes shares one label.
-- Must stay a SINGLE statement: the runner reads the number of updated rows after each execution.
-- =============================================================================

MERGE INTO IDENTITY_LABELS t
USING (
    SELECT e.NODE_ID, MIN(l.CLUSTER_LABEL) AS NEW_LABEL
    FROM (
        SELECT NODE_A AS NODE_ID, NODE_B AS OTHER_ID FROM IDENTITY_PAIR_SCORES WHERE IS_MERGED = 1
        UNION ALL
        SELECT NODE_B, NODE_A FROM IDENTITY_PAIR_SCORES WHERE IS_MERGED = 1
    ) e
    JOIN IDENTITY_LABELS l ON l.NODE_ID = e.OTHER_ID
    GROUP BY e.NODE_ID
) s ON t.NODE_ID = s.NODE_ID
WHEN MATCHED THEN UPDATE SET t.CLUSTER_LABEL = s.NEW_LABEL WHERE s.NEW_LABEL < t.CLUSTER_LABEL;
