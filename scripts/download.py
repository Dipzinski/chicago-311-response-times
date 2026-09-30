"""Download Chicago 311 requests (5 fix-it request types, 2019 onward) into DuckDB.

Sources (City of Chicago Data Portal, Socrata API, no key needed):
  - 311 Service Requests (v6vf-nfxy)
  - Boundaries - Community Areas (igwz-8jzy), for maps and area names
  - ACS 5 Year Data by Community Area - Most Recent Year (7umk-8dtw), for income

Usage:  python scripts/download.py
Output: data/raw/311/*.csv, data/raw/acs_community_areas.csv,
        data/community_areas.geojson, data/311.duckdb
Optional: set SOCRATA_APP_TOKEN to get a higher API rate limit.
"""
import csv
import io
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
import requests

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
RAW = DATA / "raw"
RAW_311 = RAW / "311"
DB_PATH = DATA / "311.duckdb"

API = "https://data.cityofchicago.org/resource"
REQUESTS_ID = "v6vf-nfxy"
BOUNDARIES_ID = "igwz-8jzy"
ACS_ID = "7umk-8dtw"

REQUEST_TYPES = [
    "Graffiti Removal Request",
    "Pothole in Street Complaint",
    "Street Light Out Complaint",
    "Rodent Baiting/Rat Complaint",
    "Abandoned Vehicle Complaint",
]
START = "2019-01-01T00:00:00"
COLUMNS = [
    "sr_number", "sr_type", "owner_department", "status", "origin",
    "created_date", "closed_date", "duplicate", "legacy_record", "parent_sr_number",
    "community_area", "ward", "latitude", "longitude",
]
PAGE_SIZE = 50_000

HEADERS = {"X-App-Token": os.environ["SOCRATA_APP_TOKEN"]} if os.environ.get("SOCRATA_APP_TOKEN") else {}


def get(url, params=None, tries=8):
    """GET with retries; the portal sometimes answers 500/503 or times out when it is busy."""
    for attempt in range(1, tries + 1):
        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=300)
            if resp.status_code == 200:
                return resp
            error = f"HTTP {resp.status_code}: {resp.text[:200]}"
        except requests.RequestException as exc:
            error = str(exc)
        wait = min(60, 5 * 2 ** (attempt - 1))  # 5, 10, 20, 40, 60, 60... seconds
        print(f"  attempt {attempt} failed ({error}); retrying in {wait}s", flush=True)
        time.sleep(wait)
    raise RuntimeError(f"Giving up on {url}: {error}")


def display_name(name):
    """ROGERS PARK -> Rogers Park, fixing the two names that str.title() gets wrong."""
    return {"OHARE": "O'Hare", "MCKINLEY PARK": "McKinley Park"}.get(name, name.title())


def slug(text):
    return "".join(c if c.isalnum() else "_" for c in text.lower()).strip("_")


def download_type(sr_type, where_time):
    """Page through one request type in sr_number order (keyset pagination).

    Keyset pagination ("sr_number > last one seen") stays correct even though the
    city updates the dataset while we download, unlike $offset paging.
    """
    base_where = f"sr_type = '{sr_type}' AND {where_time}"
    expected = int(get(f"{API}/{REQUESTS_ID}.json",
                       {"$select": "count(*) AS n", "$where": base_where}).json()[0]["n"])
    out_path = RAW_311 / f"{slug(sr_type)}.csv"
    last, rows = None, 0
    with open(out_path, "w", newline="") as out:
        writer = csv.writer(out)
        writer.writerow(COLUMNS)
        while True:
            where = base_where if last is None else f"{base_where} AND sr_number > '{last}'"
            resp = get(f"{API}/{REQUESTS_ID}.csv", {
                "$select": ",".join(COLUMNS),
                "$where": where,
                "$order": "sr_number",
                "$limit": PAGE_SIZE,
            })
            page = list(csv.reader(io.StringIO(resp.text)))[1:]  # drop the header row
            if not page:
                break
            writer.writerows(page)
            rows += len(page)
            last = page[-1][0]
            print(f"  {sr_type}: {rows:,} of {expected:,}", flush=True)
            if len(page) < PAGE_SIZE:
                break
    return expected, rows


def main():
    RAW_311.mkdir(parents=True, exist_ok=True)
    # Stop at midnight (Chicago time) so the row counts and the pages describe the same snapshot:
    # the city keeps adding today's requests while the download runs, but yesterday is settled.
    today = datetime.now(ZoneInfo("America/Chicago")).date()
    end = datetime(today.year, today.month, today.day)
    where_time = f"created_date >= '{START}' AND created_date < '{end.isoformat()}'"
    print(f"Downloading 311 requests created {START[:10]} through {today - timedelta(days=1)}")

    # The API sends about 1,000 rows a second per request, so fetch the 5 types in parallel.
    with ThreadPoolExecutor(max_workers=len(REQUEST_TYPES)) as pool:
        counts = list(pool.map(lambda sr_type: download_type(sr_type, where_time), REQUEST_TYPES))
    log = [(sr_type, expected, rows, end) for sr_type, (expected, rows) in zip(REQUEST_TYPES, counts)]

    print("Downloading community-area boundaries and ACS income data")
    geojson = get(f"{API}/{BOUNDARIES_ID}.geojson", {"$limit": 100}).text
    (DATA / "community_areas.geojson").write_text(geojson)
    (RAW / "acs_community_areas.csv").write_text(get(f"{API}/{ACS_ID}.csv", {"$limit": 1000}).text)

    con = duckdb.connect(str(DB_PATH))
    con.execute(f"""
        CREATE OR REPLACE TABLE raw_requests AS
        SELECT * FROM read_csv('{RAW_311}/*.csv', header = true, columns = {{
            'sr_number': 'VARCHAR', 'sr_type': 'VARCHAR', 'owner_department': 'VARCHAR',
            'status': 'VARCHAR', 'origin': 'VARCHAR',
            'created_date': 'TIMESTAMP', 'closed_date': 'TIMESTAMP',
            'duplicate': 'BOOLEAN', 'legacy_record': 'BOOLEAN', 'parent_sr_number': 'VARCHAR',
            'community_area': 'INTEGER', 'ward': 'INTEGER',
            'latitude': 'DOUBLE', 'longitude': 'DOUBLE'}})
    """)
    areas = sorted((int(f["properties"]["area_numbe"]), display_name(f["properties"]["community"]))
                   for f in json.loads(geojson)["features"])
    con.execute("CREATE OR REPLACE TABLE community_areas (community_area INTEGER, name VARCHAR)")
    con.executemany("INSERT INTO community_areas VALUES (?, ?)", areas)
    con.execute(f"""
        CREATE OR REPLACE TABLE acs_income AS
        SELECT * FROM read_csv('{RAW}/acs_community_areas.csv', header = true)
    """)
    con.execute("""CREATE OR REPLACE TABLE download_log
                   (sr_type VARCHAR, api_count BIGINT, rows_downloaded BIGINT, downloaded_before TIMESTAMP)""")
    con.executemany("INSERT INTO download_log VALUES (?, ?, ?, ?)", log)

    print(con.sql("SELECT sr_type, api_count, rows_downloaded FROM download_log"))
    total = con.sql("SELECT count(*) FROM raw_requests").fetchone()[0]
    print(f"Loaded {total:,} requests into {DB_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
