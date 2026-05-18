#!/usr/bin/env bash
# Decode credential env vars into the paths the skill expects, install Python
# deps, and verify the Metabase key + Propelix CLI work. Idempotent. Run once
# at the start of each routine fire.
set -euo pipefail

echo "[bootstrap] starting"

: "${FRONT_API_TOKEN_B64:?FRONT_API_TOKEN_B64 env var required}"
: "${PROPELIX_CONFIG_B64:?PROPELIX_CONFIG_B64 env var required}"
: "${GOOGLE_SA_B64:?GOOGLE_SA_B64 env var required}"
: "${METABASE_DATA_SHEET_ID:?METABASE_DATA_SHEET_ID env var required (master sheet id)}"
# METABASE_API_KEY is not required — the routine reads pre-pulled Metabase data from
# hidden master sheet tabs (Apps Script time-trigger handles the actual queries
# because Jerry's Metabase blocks Anthropic cloud IPs). Local laptop runs use
# AWS SSM or set METABASE_API_KEY explicitly; remote routine runs use the sheet.

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

# Sanity: master sheet reachable via the service account.
python3 - <<'PY'
import os, json
from google.oauth2.service_account import Credentials
import gspread
creds = Credentials.from_service_account_file(
    os.path.expanduser("~/.claude/google-sheets-sa.json"),
    scopes=["https://www.googleapis.com/auth/spreadsheets"],
)
gc = gspread.authorize(creds)
sh = gc.open_by_key(os.environ["METABASE_DATA_SHEET_ID"])
print(f"[bootstrap] Master sheet OK: {sh.title}")
PY

# Sanity: propelix-cli reachable.
npx -y propelix-cli@latest --version >/dev/null 2>&1 && echo "[bootstrap] propelix-cli OK" || {
  echo "[bootstrap] WARNING: propelix-cli --version failed; will retry in the bot puller"
}

# Set up run dir base
mkdir -p "$HOME/escalation_clusters/runs"

echo "[bootstrap] done"
