-- 01_clean.sql: apply the cleaning rules.
-- One row per downloaded request. exclude_reason is NULL for requests kept in the analysis;
-- otherwise it names the first rule that removed the request (rules are checked in order).

CREATE OR REPLACE TABLE requests AS
SELECT
    sr_number,
    sr_type,
    status,
    origin,
    created_date,
    closed_date,
    year(created_date) AS year,
    community_area,
    ward,
    latitude,
    longitude,
    date_diff('second', created_date, closed_date) / 86400.0 AS days_to_close,
    CASE
        -- Imported from the 311 system the city replaced in December 2018.
        WHEN legacy_record THEN 'legacy record'
        -- A repeat report of a problem that already has an open request (the parent).
        -- It closes when the parent closes, so counting it would double-count one repair.
        WHEN duplicate THEN 'duplicate'
        -- Opened by city staff to log their own work (for example, graffiti crews entering
        -- the walls they just cleaned), not by residents waiting for service.
        WHEN origin IN ('Mass Entry', 'Generated In House', 'WOFromTerraGo') THEN 'city-logged work'
        WHEN status = 'Canceled' THEN 'canceled'
        WHEN closed_date < created_date THEN 'closed before it was opened'
        -- Too fast for a crew to be sent out. These happen almost only during the workday, and
        -- mostly on pothole requests entered by phone (45% of phone requests opened 7 AM-3 PM vs
        -- 0.3% from other channels), so they look like city staff logging work already done.
        -- See section 2 of analysis.ipynb.
        WHEN closed_date < created_date + INTERVAL 1 HOUR THEN 'closed within 1 hour'
    END AS exclude_reason
FROM raw_requests;

-- How many requests each rule removed, per request type, for the README and the dashboard.
CREATE OR REPLACE TABLE cleaning_summary AS
SELECT
    sr_type,
    coalesce(exclude_reason, 'kept') AS outcome,
    count(*) AS requests,
    round(100.0 * count(*) / sum(count(*)) OVER (PARTITION BY sr_type), 1) AS pct_of_type
FROM requests
GROUP BY sr_type, outcome
ORDER BY sr_type, requests DESC;
