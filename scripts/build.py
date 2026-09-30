"""Clean the raw data, build the summary tables, run the data-quality checks, and write exports.

Usage:  python scripts/build.py   (after scripts/download.py)
Steps:  sql/01_clean.sql -> sql/02_metrics.sql -> sql/03_checks.sql (stops if a check fails)
        -> exports/*.csv and exports/community_areas.geojson (for the notebook, Tableau, or Excel)
        -> docs/data.json (for the dashboard page in docs/index.html)
"""
import json
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "311.duckdb"
SQL = ROOT / "sql"
EXPORTS = ROOT / "exports"
DOCS = ROOT / "docs"
EXPORT_TABLES = ["area_year_type", "city_year_type", "cleaning_summary", "area_income"]
SHORT_NAMES = {
    "Pothole in Street Complaint": "Potholes",
    "Street Light Out Complaint": "Street lights",
    "Graffiti Removal Request": "Graffiti",
    "Rodent Baiting/Rat Complaint": "Rats",
    "Abandoned Vehicle Complaint": "Abandoned vehicles",
}


def simplify_geojson(src, dst, names, digits=4):
    """Round coordinates to about 10 m and drop repeated points so the map file stays small."""
    geo = json.loads(Path(src).read_text())

    def ring(points):
        out = []
        for lon, lat in points:
            point = [round(lon, digits), round(lat, digits)]
            if not out or point != out[-1]:
                out.append(point)
        return out

    for feature in geo["features"]:
        feature["geometry"]["coordinates"] = [
            [ring(r) for r in polygon] for polygon in feature["geometry"]["coordinates"]
        ]
        area = int(feature["properties"]["area_numbe"])
        feature["properties"] = {"community_area": area, "name": names[area]}
    Path(dst).write_text(json.dumps(geo, separators=(",", ":")))


def write_dashboard_data(con, names):
    """Bundle the summary tables and the map into one JSON file for docs/index.html."""
    def records(sql):
        cursor = con.execute(sql)
        columns = [c[0] for c in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    data_through = con.execute(
        "SELECT strftime(max(downloaded_before) - INTERVAL 1 DAY, '%B %-d, %Y') FROM download_log").fetchone()[0]
    partial_year = con.execute("SELECT year(max(downloaded_before) - INTERVAL 1 DAY) FROM download_log").fetchone()[0]
    years = [y for (y,) in con.execute("SELECT DISTINCT year FROM city_year_type ORDER BY year").fetchall()]
    data = {
        "data_through": data_through,
        "types": [{"key": key, "label": label} for key, label in SHORT_NAMES.items()],
        "years": years,
        "partial_year": partial_year,
        "last_full_year": partial_year - 1,
        "area_names": names,
        "areas": records("""SELECT sr_type AS type, community_area AS area, year, requests,
                                   median_days AS median, p90_days AS p90
                            FROM area_year_type"""),
        "city": records("""SELECT sr_type AS type, year, requests, still_open,
                                  median_days AS median, p90_days AS p90
                           FROM city_year_type"""),
        "geo": json.loads((EXPORTS / "community_areas.geojson").read_text()),
    }
    DOCS.mkdir(exist_ok=True)
    (DOCS / "data.json").write_text(json.dumps(data, separators=(",", ":")))


def main():
    con = duckdb.connect(str(DB_PATH))
    for name in ["01_clean.sql", "02_metrics.sql"]:
        con.execute((SQL / name).read_text())
        print(f"Ran sql/{name}")

    print("Data-quality checks:")
    failed = 0
    for check, failing, allowed in con.execute((SQL / "03_checks.sql").read_text()).fetchall():
        ok = failing <= allowed
        failed += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {check}  (failing rows: {failing:,}; allowed: {allowed:,})")
    if failed:
        raise SystemExit(f"{failed} check(s) failed; fix the data or the rules before using the results.")

    EXPORTS.mkdir(exist_ok=True)
    for table in EXPORT_TABLES:
        con.execute(f"COPY {table} TO '{EXPORTS / table}.csv' (HEADER)")
    names = dict(con.execute("SELECT community_area, name FROM community_areas").fetchall())
    simplify_geojson(ROOT / "data" / "community_areas.geojson", EXPORTS / "community_areas.geojson", names)
    print(f"Wrote {len(EXPORT_TABLES)} CSV files and community_areas.geojson to exports/")
    write_dashboard_data(con, names)
    print("Wrote docs/data.json for the dashboard")

    raw, kept = con.execute("""SELECT count(*), count(*) FILTER (WHERE exclude_reason IS NULL)
                               FROM requests""").fetchone()
    print(f"{raw:,} requests downloaded; {kept:,} kept after cleaning ({kept / raw:.1%}).")


if __name__ == "__main__":
    main()
