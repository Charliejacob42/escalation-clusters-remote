#!/usr/bin/env python3
"""Execute a ClickHouse query via Metabase API and save results as JSON."""
import json
import os
import subprocess
import sys
import requests

METABASE_URL = "https://metabase.ing.getjerry.com"
DATABASE_ID = 8  # ClickHouse production

def get_api_key():
    # Prefer METABASE_API_KEY env var (remote routines, CI). Fall back to AWS SSM
    # for local laptop runs where the key is stored under the jerry profile.
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
    # Metabase /api/dataset defaults to a 2000-row cap that silently truncates
    # any larger result set. Override with explicit constraints to return the
    # full result. 1M is well above any real reporting query we run.
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

if __name__ == "__main__":
    sql_file = sys.argv[1]
    out_file = sys.argv[2] if len(sys.argv) > 2 else sql_file.replace(".sql", "_results.json")

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
    print(f"Saved to {out_file}")
