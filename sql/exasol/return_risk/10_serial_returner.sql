-- =============================================================================
-- Serial Returner batch job: Return Risk Level (1-5) per customer.
--
-- Runs over the Golden Record, so purchases and returns made under a Shopify account AND under the
-- guest devices stitched to it are counted together. The result is written to USER_RETURN_RISK with
-- one row per Shopify user id / guest device id, which is what a checkout request carries.
-- Celery Beat triggers this job and later exports the table to PostgreSQL; Exasol never does either.
--
-- Return rate = purchases (last ${WINDOW_MONTHS} months) that were later returned
--               / purchases in the last ${WINDOW_MONTHS} months.
-- Levels (PRD defines 1, 3 and 5; 2 and 4 fill the gaps):
--   1: rate < ${LEVEL1_MAX}   2: < ${LEVEL2_MAX}   3: < ${LEVEL3_MAX}   4: <= ${LEVEL4_MAX}   5: above
-- A customer with fewer than ${MIN_PURCHASES} purchases in the window has too little history to judge
-- and stays at level 1 (one return out of one purchase must not read as a 100% serial returner).
-- =============================================================================

DELETE FROM USER_RETURN_RISK;

INSERT INTO USER_RETURN_RISK
    (USER_ID, GOLDEN_CUSTOMER_ID, RISK_LEVEL, RETURN_RATE, PURCHASES_6M, RETURNS_6M)
SELECT USER_ID, GOLDEN_CUSTOMER_ID,
       CASE WHEN PURCHASE_COUNT < ${MIN_PURCHASES} THEN 1
            WHEN RETURN_RATE < ${LEVEL1_MAX} THEN 1
            WHEN RETURN_RATE < ${LEVEL2_MAX} THEN 2
            WHEN RETURN_RATE < ${LEVEL3_MAX} THEN 3
            WHEN RETURN_RATE <= ${LEVEL4_MAX} THEN 4
            ELSE 5 END,
       RETURN_RATE, PURCHASE_COUNT, RETURNED_COUNT
FROM (
    SELECT ids.USER_ID, ids.GOLDEN_CUSTOMER_ID, s.PURCHASE_COUNT, s.RETURNED_COUNT,
           CASE WHEN s.PURCHASE_COUNT = 0 THEN 0
                ELSE CAST(s.RETURNED_COUNT AS DOUBLE) / s.PURCHASE_COUNT END AS RETURN_RATE
    FROM (
        -- Every id a checkout can carry: Shopify user ids and guest device ids.
        SELECT m.GOLDEN_CUSTOMER_ID, n.SOURCE_ID AS USER_ID
        FROM GOLDEN_CUSTOMER_MEMBERS m
        JOIN IDENTITY_NODES n ON n.NODE_ID = m.NODE_ID
        WHERE n.NODE_TYPE IN ('SHOPIFY', 'DEVICE')
    ) ids
    JOIN (
        -- Window statistics per golden customer, summed over all of its ids.
        SELECT m.GOLDEN_CUSTOMER_ID,
               COUNT(DISTINCT p.PURCHASE_ID) AS PURCHASE_COUNT,
               COUNT(DISTINCT r.PURCHASE_ID) AS RETURNED_COUNT
        FROM GOLDEN_CUSTOMER_MEMBERS m
        JOIN IDENTITY_NODES n ON n.NODE_ID = m.NODE_ID AND n.NODE_TYPE IN ('SHOPIFY', 'DEVICE')
        LEFT JOIN RAW_PURCHASES p
               ON p.USER_ID = n.SOURCE_ID AND p.PURCHASED_AT >= ADD_MONTHS(CURRENT_TIMESTAMP, -${WINDOW_MONTHS})
        LEFT JOIN RAW_RETURNS r ON r.PURCHASE_ID = p.PURCHASE_ID
        GROUP BY m.GOLDEN_CUSTOMER_ID
    ) s ON s.GOLDEN_CUSTOMER_ID = ids.GOLDEN_CUSTOMER_ID
) scored;
