-- 02_metrics.sql: days-to-close summaries for the notebook and the dashboard.
-- Open requests count toward `requests` but have no days_to_close, so the medians use
-- closed requests only. The current year is partial, so trend charts stop at the last full year.

CREATE OR REPLACE VIEW kept AS
SELECT * FROM requests WHERE exclude_reason IS NULL;

-- Median and 90th-percentile days to close by request type, community area, and year.
CREATE OR REPLACE TABLE area_year_type AS
SELECT
    k.sr_type,
    k.community_area,
    a.name AS community_area_name,
    k.year,
    count(*) AS requests,
    count(k.closed_date) AS closed,
    count(*) - count(k.closed_date) AS still_open,
    round(median(k.days_to_close), 2) AS median_days,
    round(quantile_cont(k.days_to_close, 0.9), 2) AS p90_days
FROM kept k
JOIN community_areas a USING (community_area)
GROUP BY ALL
ORDER BY k.sr_type, k.community_area, k.year;

-- Income by community area: share of families earning under $50,000 a year
-- (American Community Survey 5-year estimates, as aggregated to community areas by the city).
-- The ACS file names areas in capitals without numbers, so match on letters only (O'HARE = OHARE).
CREATE OR REPLACE TABLE area_income AS
SELECT
    a.community_area,
    a.name AS community_area_name,
    i.acs_year,
    i.total_population,
    i.under_25_000 + i._25_000_to_49_999 + i._50_000_to_74_999 + i._75_000_to_125_000 + i._125_000 AS families,
    round(100.0 * (i.under_25_000 + i._25_000_to_49_999) / families, 1) AS pct_families_under_50k
FROM community_areas a
JOIN acs_income i
  ON regexp_replace(upper(a.name), '[^A-Z]', '', 'g') = regexp_replace(upper(i.community_area), '[^A-Z]', '', 'g')
ORDER BY a.community_area;

-- The same summary for the whole city (includes the few requests with no community area).
CREATE OR REPLACE TABLE city_year_type AS
SELECT
    sr_type,
    year,
    count(*) AS requests,
    count(closed_date) AS closed,
    count(*) - count(closed_date) AS still_open,
    round(median(days_to_close), 2) AS median_days,
    round(quantile_cont(days_to_close, 0.9), 2) AS p90_days
FROM kept
GROUP BY ALL
ORDER BY sr_type, year;
