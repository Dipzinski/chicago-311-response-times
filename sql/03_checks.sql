-- 03_checks.sql: automated data-quality checks, run by scripts/build.py after every build.
-- Each row is one check: failing_rows must be <= allowed, or the build stops.

WITH
raw_n  AS (SELECT count(*) AS n FROM raw_requests),
kept_n AS (SELECT count(*) AS n FROM requests WHERE exclude_reason IS NULL)

SELECT '1. sr_number is unique' AS check_name,
       (SELECT count(*) - count(DISTINCT sr_number) FROM raw_requests) AS failing_rows,
       0 AS allowed

UNION ALL
SELECT '2. rows loaded match the API count for each type',
       (SELECT count(*) FROM download_log d
        WHERE d.rows_downloaded <> d.api_count
           OR d.api_count <> (SELECT count(*) FROM raw_requests r WHERE r.sr_type = d.sr_type)),
       0

UNION ALL
SELECT '3. only the 5 requested request types',
       (SELECT count(*) FROM raw_requests WHERE sr_type NOT IN (SELECT sr_type FROM download_log)),
       0

UNION ALL
SELECT '4. created_date is inside the download window',
       (SELECT count(*) FROM raw_requests
        WHERE created_date IS NULL
           OR created_date < TIMESTAMP '2019-01-01'
           OR created_date >= (SELECT max(downloaded_before) FROM download_log)),
       0

UNION ALL
SELECT '5. status is Open, Completed, or Canceled',
       (SELECT count(*) FROM raw_requests
        WHERE status IS NULL OR status NOT IN ('Open', 'Completed', 'Canceled')),
       0

UNION ALL
SELECT '6. every completed request has a closed_date',
       (SELECT count(*) FROM raw_requests WHERE status = 'Completed' AND closed_date IS NULL),
       0

UNION ALL
SELECT '7. community_area is between 1 and 77 when present',
       (SELECT count(*) FROM raw_requests WHERE community_area NOT BETWEEN 1 AND 77),
       0

UNION ALL
SELECT '8. at most 1% of kept requests lack a community area',
       (SELECT count(*) FROM requests WHERE exclude_reason IS NULL AND community_area IS NULL),
       (SELECT n // 100 FROM kept_n)

UNION ALL
SELECT '9. cleaning keeps every raw row exactly once',
       (SELECT abs(count(*) - (SELECT n FROM raw_n)) FROM requests)
     + (SELECT count(*) - count(DISTINCT sr_number) FROM requests),
       0

UNION ALL
SELECT '10. coordinates fall inside the Chicago area when present',
       (SELECT count(*) FROM raw_requests
        WHERE latitude IS NOT NULL
          AND (latitude NOT BETWEEN 41.60 AND 42.05 OR longitude NOT BETWEEN -87.96 AND -87.50)),
       0

UNION ALL
SELECT '11. all 77 community areas have a boundary and ACS income data',
       77 - (SELECT count(*) FROM community_areas a JOIN area_income i USING (community_area)),
       0;
