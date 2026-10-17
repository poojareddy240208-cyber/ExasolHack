-- =============================================================================
-- Identity stitching, stage 3: weighted probabilistic scoring of candidate pairs.
--
--   LOGIT       = BIAS + EMAIL_WEIGHT * EMAIL_MATCH + LINK_WEIGHT * ACCOUNT_LINK + PROMO_WEIGHT
--   MATCH_SCORE = 1 / (1 + EXP(-LOGIT))          -- probability the two nodes are one person
--   IS_MERGED   = MATCH_SCORE >= MERGE_THRESHOLD
--
-- BIAS is a strongly negative prior ("two random nodes are almost never the same person").
-- PROMO_WEIGHT is capped at ${PROMO_WEIGHT_CAP}, which is smaller than |BIAS|, so promo evidence alone
-- can never reach the threshold. It only adds confidence on top of email or login evidence.
-- =============================================================================

DELETE FROM IDENTITY_PROMO_PAIRS;

-- A promo code shared by few nodes is mild evidence (rarer code = more weight, like IDF in search).
-- Coupon-site codes were already removed in stage 2, so every code here is rare.
INSERT INTO IDENTITY_PROMO_PAIRS (NODE_A, NODE_B, PROMO_WEIGHT)
SELECT a.NODE_ID, b.NODE_ID,
       MAX(LEAST(${PROMO_WEIGHT_CAP}, LN(CAST(t.TOTAL_NODES AS DOUBLE) / h.HOLDERS)))
FROM IDENTITY_NODE_PROMOS a
JOIN IDENTITY_NODE_PROMOS b ON a.PROMO_CODE = b.PROMO_CODE AND a.NODE_ID < b.NODE_ID
JOIN (SELECT PROMO_CODE, COUNT(*) AS HOLDERS FROM IDENTITY_NODE_PROMOS GROUP BY PROMO_CODE) h
     ON h.PROMO_CODE = a.PROMO_CODE
CROSS JOIN (SELECT COUNT(*) AS TOTAL_NODES FROM IDENTITY_NODES) t
GROUP BY a.NODE_ID, b.NODE_ID;

DELETE FROM IDENTITY_PAIR_SCORES;

INSERT INTO IDENTITY_PAIR_SCORES
    (NODE_A, NODE_B, EMAIL_MATCH, ACCOUNT_LINK, PROMO_WEIGHT, LOGIT, MATCH_SCORE, IS_MERGED)
SELECT NODE_A, NODE_B, EMAIL_MATCH, ACCOUNT_LINK, PROMO_WEIGHT, LOGIT,
       1 / (1 + EXP(-LOGIT)),
       CASE WHEN 1 / (1 + EXP(-LOGIT)) >= ${MERGE_THRESHOLD} THEN 1 ELSE 0 END
FROM (
    SELECT NODE_A, NODE_B, EMAIL_MATCH, ACCOUNT_LINK, PROMO_WEIGHT,
           (${BIAS}) + ${EMAIL_WEIGHT} * EMAIL_MATCH + ${LINK_WEIGHT} * ACCOUNT_LINK + PROMO_WEIGHT AS LOGIT
    FROM (
        SELECT c.NODE_A, c.NODE_B,
               CASE WHEN a.EMAIL IS NOT NULL AND a.EMAIL = b.EMAIL THEN 1 ELSE 0 END AS EMAIL_MATCH,
               CASE WHEN l.NODE_A IS NOT NULL THEN 1 ELSE 0 END AS ACCOUNT_LINK,
               COALESCE(p.PROMO_WEIGHT, 0) AS PROMO_WEIGHT
        FROM (
            -- Candidate pairs: only nodes that share some evidence are ever compared.
            SELECT x.NODE_ID AS NODE_A, y.NODE_ID AS NODE_B
            FROM IDENTITY_NODES x
            JOIN IDENTITY_NODES y ON x.EMAIL = y.EMAIL AND x.NODE_ID < y.NODE_ID
            UNION
            SELECT NODE_A, NODE_B FROM IDENTITY_LINKS
            UNION
            SELECT NODE_A, NODE_B FROM IDENTITY_PROMO_PAIRS
        ) c
        JOIN IDENTITY_NODES a ON a.NODE_ID = c.NODE_A
        JOIN IDENTITY_NODES b ON b.NODE_ID = c.NODE_B
        LEFT JOIN (SELECT DISTINCT NODE_A, NODE_B FROM IDENTITY_LINKS) l
               ON l.NODE_A = c.NODE_A AND l.NODE_B = c.NODE_B
        LEFT JOIN IDENTITY_PROMO_PAIRS p
               ON p.NODE_A = c.NODE_A AND p.NODE_B = c.NODE_B
    ) features
) scored;
