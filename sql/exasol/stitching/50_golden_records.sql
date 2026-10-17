-- =============================================================================
-- Identity stitching, stage 5: publish the Golden Record.
-- The id is derived from the cluster label, so an unchanged cluster keeps the same id between runs.
-- =============================================================================

DELETE FROM GOLDEN_CUSTOMER_MEMBERS;
DELETE FROM GOLDEN_CUSTOMERS;

-- Which fragments belong to which golden customer. CONFIDENCE is the strongest merge score that
-- connects the node to the cluster (1.0 for a node that stands alone).
INSERT INTO GOLDEN_CUSTOMER_MEMBERS (GOLDEN_CUSTOMER_ID, NODE_ID, CONFIDENCE)
SELECT 'CUST-GOLDEN-' || UPPER(SUBSTR(HASH_MD5(l.CLUSTER_LABEL), 1, 10)),
       l.NODE_ID,
       COALESCE(c.CONFIDENCE, 1)
FROM IDENTITY_LABELS l
LEFT JOIN (
    SELECT NODE_ID, MAX(MATCH_SCORE) AS CONFIDENCE
    FROM (
        SELECT NODE_A AS NODE_ID, MATCH_SCORE FROM IDENTITY_PAIR_SCORES WHERE IS_MERGED = 1
        UNION ALL
        SELECT NODE_B, MATCH_SCORE FROM IDENTITY_PAIR_SCORES WHERE IS_MERGED = 1
    ) m
    GROUP BY NODE_ID
) c ON c.NODE_ID = l.NODE_ID;

-- One summary row per customer. Spend is net of refunds and counts purchases made under any
-- Shopify account or guest device in the cluster.
INSERT INTO GOLDEN_CUSTOMERS
    (GOLDEN_CUSTOMER_ID, PRIMARY_EMAIL, MEMBER_COUNT, TOTAL_SPEND_USD, FIRST_SEEN_AT, LAST_SEEN_AT)
SELECT m.GOLDEN_CUSTOMER_ID,
       COALESCE(MAX(CASE WHEN n.NODE_TYPE = 'SHOPIFY' THEN n.EMAIL END), MIN(n.EMAIL)),
       COUNT(*),
       COALESCE(SUM(s.NET_SPEND), 0),
       MIN(n.FIRST_SEEN),
       MAX(n.LAST_SEEN)
FROM GOLDEN_CUSTOMER_MEMBERS m
JOIN IDENTITY_NODES n ON n.NODE_ID = m.NODE_ID
LEFT JOIN (
    SELECT p.USER_ID, SUM(p.QUANTITY * p.UNIT_PRICE) - COALESCE(MAX(r.REFUNDED), 0) AS NET_SPEND
    FROM RAW_PURCHASES p
    LEFT JOIN (SELECT USER_ID, SUM(REFUND_AMOUNT) AS REFUNDED FROM RAW_RETURNS GROUP BY USER_ID) r
           ON r.USER_ID = p.USER_ID
    GROUP BY p.USER_ID
) s ON s.USER_ID = n.SOURCE_ID AND n.NODE_TYPE IN ('SHOPIFY', 'DEVICE')
GROUP BY m.GOLDEN_CUSTOMER_ID;
