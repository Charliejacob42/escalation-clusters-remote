#!/usr/bin/env bash
# Decode credential env vars into the paths the skill expects, install Python
# deps, and verify the Metabase key + Propelix CLI work. Idempotent. Run once
# at the start of each routine fire.
set -euo pipefail

echo "[bootstrap] starting"

: "${METABASE_API_KEY:?METABASE_API_KEY env var required}"
: "${FRONT_API_TOKEN_B64:?FRONT_API_TOKEN_B64 env var required}"
: "${PROPELIX_CONFIG_B64:?PROPELIX_CONFIG_B64 env var required}"
: "${GOOGLE_SA_B64:?GOOGLE_SA_B64 env var required}"

mkdir -p "$HOME/.claude" "$HOME/.config/propelix"

echo "$FRONT_API_TOKEN_B64" | base64 -d > "$HOME/.claude/front-api-token.txt"
chmod 600 "$HOME/.claude/front-api-token.txt"

echo "$PROPELIX_CONFIG_B64" | base64 -d > "$HOME/.config/propelix/config.json"
chmod 600 "$HOME/.config/propelix/config.json"

echo "$GOOGLE_SA_B64" | base64 -d > "$HOME/.claude/google-sheets-sa.json"
chmod 600 "$HOME/.claude/google-sheets-sa.json"

echo "[bootstrap] credential files placed"

# Install Python deps quietly. The skill expects gspread + google-auth +
# google-api-python-client + requests + zoneinfo (stdlib on >=3.9).
pip install --quiet --disable-pip-version-check \
    gspread google-auth google-api-python-client requests

echo "[bootstrap] python deps installed"

# Sanity: Metabase key works.
python3 - <<'PY'
import os, requests, sys
r = requests.post(
    "https://metabase.ing.getjerry.com/api/dataset",
    headers={"X-API-KEY": os.environ["METABASE_API_KEY"], "Content-Type": "application/json"},
    json={"database": 8, "type": "native", "native": {"query": "SELECT 1 AS ok"},
          "constraints": {"max-results": 1, "max-results-bare-rows": 1}},
    timeout=30,
)
r.raise_for_status()
rows = r.json().get("data", {}).get("rows", [])
print(f"[bootstrap] Metabase OK: {rows}")
PY

# Sanity: propelix-cli reachable.
npx -y propelix-cli@latest --version >/dev/null 2>&1 && echo "[bootstrap] propelix-cli OK" || {
  echo "[bootstrap] WARNING: propelix-cli --version failed; will retry in the bot puller"
}

# Set up run dir base
mkdir -p "$HOME/escalation_clusters/runs"

echo "[bootstrap] done"
