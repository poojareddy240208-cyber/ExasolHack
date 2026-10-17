-- =============================================================================
-- Identity stitching, stage 1: build one node per identity fragment.
-- Rebuilt from scratch on every run. Dollar-brace placeholders are filled in by
-- backend/services/identity_stitching.py.
-- =============================================================================

DELETE FROM IDENTITY_NODES;

-- Registered Shopify accounts.
INSERT INTO IDENTITY_NODES (NODE_ID, NODE_TYPE, SOURCE_ID, EMAIL, PHONE, FULL_NAME, FIRST_SEEN, LAST_SEEN)
SELECT 'SHOPIFY:' || u.USER_ID, 'SHOPIFY', u.USER_ID, u.EMAIL, u.PHONE, u.FULL_NAME,
       u.CREATED_AT, COALESCE(p.LAST_PURCHASE, u.CREATED_AT)
FROM RAW_USERS u
LEFT JOIN (SELECT USER_ID, MAX(PURCHASED_AT) AS LAST_PURCHASE FROM RAW_PURCHASES GROUP BY USER_ID) p
       ON p.USER_ID = u.USER_ID;

-- Anonymous devices. The unstructured JSON payload is parsed here: a device that typed its email
-- into a sign-up box carries it as "email_hint".
INSERT INTO IDENTITY_NODES (NODE_ID, NODE_TYPE, SOURCE_ID, EMAIL, PHONE, FULL_NAME, FIRST_SEEN, LAST_SEEN)
SELECT 'DEVICE:' || DEVICE_ID, 'DEVICE', DEVICE_ID,
       MIN(CAST(JSON_VALUE(PAYLOAD, '$.email_hint') AS VARCHAR(255))), NULL, NULL,
       MIN(EVENT_TS), MAX(EVENT_TS)
FROM RAW_WEB_EVENTS
GROUP BY DEVICE_ID;

-- Zendesk requesters, one per distinct (case-insensitive) requester email.
INSERT INTO IDENTITY_NODES (NODE_ID, NODE_TYPE, SOURCE_ID, EMAIL, PHONE, FULL_NAME, FIRST_SEEN, LAST_SEEN)
SELECT 'ZENDESK:' || LOWER(TRIM(REQUESTER_EMAIL)), 'ZENDESK', LOWER(TRIM(REQUESTER_EMAIL)),
       LOWER(TRIM(REQUESTER_EMAIL)), NULL, NULL, MIN(CREATED_AT), MAX(CREATED_AT)
FROM RAW_SUPPORT_TICKETS
WHERE REQUESTER_EMAIL IS NOT NULL
GROUP BY LOWER(TRIM(REQUESTER_EMAIL));

-- Normalise every email once: lower-case, trim, and drop a "+tag" so "Ann+support@x.com" equals "ann@x.com".
UPDATE IDENTITY_NODES
SET EMAIL = NULLIF(REGEXP_REPLACE(LOWER(TRIM(EMAIL)), '\+[^@]*@', '@'), '')
WHERE EMAIL IS NOT NULL;
