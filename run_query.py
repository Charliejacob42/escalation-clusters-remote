#!/usr/bin/env python3
"""Execute a ClickHouse query via Metabase API and save results as JSON.

Two modes:
  - Direct (laptop runs): hit Metabase API via X-API-KEY. Reads the key from
    METABASE_API_KEY env var or falls back to AWS SSM.
  - Sheet (remote routine runs): when METABASE_DATA_SHEET_ID and
    METABASE_DATA_START_DATE env vars are set, skip Metabase entirely and read
    the pre-pulled data from hidden tabs on the master sheet. Apps Script
    runs the actual Metabase queries (since Jerry's Metabase blocks Anthropic
    cloud IPs) and writes results to:
      _data_population_YYYY-MM-DD     for population.sql
      _data_self_serve_YYYY-MM-DD     for self_serve_query.sql
"""
import json
import os
import subprocess
import sys
import time

import requests

METABASE_URL = "https://metabase.ing.getjerry.com"
DATABASE_ID = 8  # ClickHouse production
SA_PATH_DEFAULT = "/Users/charliejacob/.claude/google-sheets-sa.json"
SHEET_TAB_WAIT_SECONDS = 60 * 60  # Wait up to 1 hour for Apps Script trigger to land the tabs
SHEET_TAB_POLL_INTERVAL = 30


def get_api_key():
    env_key = os.environ.get("METABASE_API_KEY", "").strip()
    if env_key:
        return env_key
    result = subprocess.run(
        ["aws", "ssm", "get-parameter",
         "--name", "data.METABASE_API_CODEX_DS",
         "--with-decryption",
         "--profile", "jerry",
         "--region", "us-west-2",
         "--query", "Parameter.Value",
         "--output", "text"],
        capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def execute_query(sql, api_key):
    resp = requests.post(
        f"{METABASE_URL}/api/dataset",
        headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
        json={
            "database": DATABASE_ID,
            "type": "native",
            "native": {"query": sql},
            "constraints": {
                "max-results": 1000000,
                "max-results-bare-rows": 1000000,
            },
        },
        timeout=300
    )
    resp.raise_for_status()
    return resp.json()


def metabase_to_records(data):
    cols = [c["name"] for c in data["data"]["cols"]]
    rows = data["data"]["rows"]
    return [dict(zip(cols, row)) for row in rows]


def derive_tab_name(sql_file_path, start_date):
    """Return the expected hidden-tab name for this SQL file + start date."""
    base = os.path.basename(sql_file_path).lower()
    if "population" in base:
        return f"_data_population_{start_date}"
    if "self_serve" in base:
        return f"_data_self_serve_{start_date}"
    raise ValueError(f"Cannot derive tab name from sql file {sql_file_path}; "
                     f"expected filename to contain 'population' or 'self_serve'")


def read_records_from_sheet_tab(sheet_id, tab_name, sa_path):
    """Read the rectangular tab written by Apps Script. Header row is column
    names. Data rows that start with '__JSON__' are reverse-parsed back to
    objects. Returns list of dict records identical to metabase_to_records."""
    import gspread
    from google.oauth2.service_account import Credentials

    creds = Credentials.from_service_account_file(sa_path, scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
    ])
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(sheet_id)

    deadline = time.time() + SHEET_TAB_WAIT_SECONDS
    last_err = None
    while time.time() < deadline:
        try:
            ws = sh.worksheet(tab_name)
            break
        except gspread.exceptions.WorksheetNotFound as e:
            last_err = e
            print(f"Tab '{tab_name}' not present yet; retrying in {SHEET_TAB_POLL_INTERVAL}s...",
                  flush=True)
            time.sleep(SHEET_TAB_POLL_INTERVAL)
    else:
        raise RuntimeError(f"Tab '{tab_name}' did not appear within {SHEET_TAB_WAIT_SECONDS}s: {last_err}")

    rows = ws.get_all_values()
    if not rows:
        return []
    if rows[0] == ["__EMPTY__"]:
        return []
    header = rows[0]
    out = []
    for r in rows[1:]:
        rec = {}
        for i, col in enumerate(header):
            val = r[i] if i < len(r) else ""
            if isinstance(val, str) and val.startswith("__JSON__"):
                try:
                    val = json.loads(val[len("__JSON__"):])
                except json.JSONDecodeError:
                    pass
            elif val == "":
                val = None
            rec[col] = val
        out.append(rec)
    return out


def main():
    sql_file = sys.argv[1]
    out_file = sys.argv[2] if len(sys.argv) > 2 else sql_file.replace(".sql", "_results.json")

    sheet_id = os.environ.get("METABASE_DATA_SHEET_ID", "").strip()
    start_date = os.environ.get("METABASE_DATA_START_DATE", "").strip()
    if sheet_id and start_date:
        # Sheet-tab mode: read pre-pulled data from the master sheet.
        sa_path = os.environ.get("GOOGLE_SA_PATH", SA_PATH_DEFAULT)
        tab = derive_tab_name(sql_file, start_date)
        print(f"Reading pre-pulled data from sheet tab '{tab}'...", flush=True)
        records = read_records_from_sheet_tab(sheet_id, tab, sa_path)
    else:
        # Direct mode: hit Metabase.
        with open(sql_file) as f:
            sql = f.read()
        api_key = get_api_key()
        print(f"Executing query from {sql_file}...")
        result = execute_query(sql, api_key)
        row_count = len(result["data"]["rows"])
        print(f"Got {row_count} rows")
        records = metabase_to_records(result)

    with open(out_file, "w") as f:
        json.dump(records, f, indent=2, default=str)
    print(f"Saved {len(records)} records to {out_file}")


if __name__ == "__main__":
    main()
