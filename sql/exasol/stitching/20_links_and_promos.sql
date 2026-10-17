-- =============================================================================
-- Identity stitching, stage 2: collect the evidence that can connect two nodes.
--   * account links  - a device that logged in, a ticket raised while logged in
--   * promo codes    - weak evidence only; coupon-site codes are discarded
-- =============================================================================

DELETE FROM IDENTITY_LINKS;

-- A device that sent events with a USER_ID has logged in to that Shopify account.
INSERT INTO IDENTITY_LINKS (NODE_A, NODE_B, LINK_SOURCE)
SELECT DISTINCT
       CASE WHEN w.DEVICE_NODE < w.SHOPIFY_NODE THEN w.DEVICE_NODE ELSE w.SHOPIFY_NODE END,
       CASE WHEN w.DEVICE_NODE < w.SHOPIFY_NODE THEN w.SHOPIFY_NODE ELSE w.DEVICE_NODE END,
       'WEB_LOGIN'
FROM (
    SELECT DISTINCT 'DEVICE:' || DEVICE_ID AS DEVICE_NODE, 'SHOPIFY:' || USER_ID AS SHOPIFY_NODE
    FROM RAW_WEB_EVENTS
    WHERE USER_ID IS NOT NULL
) w
JOIN IDENTITY_NODES d ON d.NODE_ID = w.DEVICE_NODE
JOIN IDENTITY_NODES s ON s.NODE_ID = w.SHOPIFY_NODE;

-- A ticket that carries a USER_ID was raised by that logged-in Shopify account.
INSERT INTO IDENTITY_LINKS (NODE_A, NODE_B, LINK_SOURCE)
SELECT DISTINCT
       CASE WHEN t.ZENDESK_NODE < t.SHOPIFY_NODE THEN t.ZENDESK_NODE ELSE t.SHOPIFY_NODE END,
       CASE WHEN t.ZENDESK_NODE < t.SHOPIFY_NODE THEN t.SHOPIFY_NODE ELSE t.ZENDESK_NODE END,
       'TICKET_USER'
FROM (
    SELECT DISTINCT 'ZENDESK:' || LOWER(TRIM(REQUESTER_EMAIL)) AS ZENDESK_NODE, 'SHOPIFY:' || USER_ID AS SHOPIFY_NODE
    FROM RAW_SUPPORT_TICKETS
    WHERE USER_ID IS NOT NULL AND REQUESTER_EMAIL IS NOT NULL
) t
JOIN IDENTITY_NODES z ON z.NODE_ID = t.ZENDESK_NODE
JOIN IDENTITY_NODES s ON s.NODE_ID = t.SHOPIFY_NODE;

-- -----------------------------------------------------------------------------
-- Promo codes used by each node
-- -----------------------------------------------------------------------------
DELETE FROM IDENTITY_NODE_PROMOS;

-- Two sources are merged and summed: codes seen in the unstructured web payloads, and codes used at
-- checkout. At checkout the buyer is a Shopify account if the id is a known user, else a guest device.
INSERT INTO IDENTITY_NODE_PROMOS (NODE_ID, PROMO_CODE, USES)
SELECT NODE_ID, PROMO_CODE, SUM(USES)
FROM (
    SELECT 'DEVICE:' || DEVICE_ID AS NODE_ID,
           UPPER(CAST(JSON_VALUE(PAYLOAD, '$.promo') AS VARCHAR(64))) AS PROMO_CODE,
           COUNT(*) AS USES
    FROM RAW_WEB_EVENTS
    WHERE JSON_VALUE(PAYLOAD, '$.promo') IS NOT NULL
    GROUP BY DEVICE_ID, UPPER(CAST(JSON_VALUE(PAYLOAD, '$.promo') AS VARCHAR(64)))
    UNION ALL
    SELECT CASE WHEN u.USER_ID IS NOT NULL THEN 'SHOPIFY:' ELSE 'DEVICE:' END || p.USER_ID,
           UPPER(p.PROMO_CODE),
           COUNT(*)
    FROM RAW_PURCHASES p
    LEFT JOIN RAW_USERS u ON u.USER_ID = p.USER_ID
    WHERE p.PROMO_CODE IS NOT NULL
    GROUP BY CASE WHEN u.USER_ID IS NOT NULL THEN 'SHOPIFY:' ELSE 'DEVICE:' END || p.USER_ID, UPPER(p.PROMO_CODE)
) promo_usage
WHERE NODE_ID IN (SELECT NODE_ID FROM IDENTITY_NODES)
GROUP BY NODE_ID, PROMO_CODE;

-- PROMO LEAKAGE DEFENCE: a code held by more than ${MAX_PROMO_HOLDERS} nodes is a coupon-site code.
-- It is dropped entirely, so it can neither create candidate pairs nor add any score.
DELETE FROM IDENTITY_NODE_PROMOS
WHERE PROMO_CODE IN (
    SELECT PROMO_CODE FROM IDENTITY_NODE_PROMOS GROUP BY PROMO_CODE HAVING COUNT(*) > ${MAX_PROMO_HOLDERS}
);
