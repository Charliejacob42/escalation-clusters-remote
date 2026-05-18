#!/usr/bin/env python3
"""Prep inputs for the opportunity-theme merger subagent.

Reads:
  - analyst_merged.json — has prompt_opportunity per record
  - classifier_merged.json — has cluster/subcluster per record
  - Opportunities tab of the master sheet (if present) — has existing themes

Writes:
  - opps_to_merge.json — this week's per-conversation opportunities (filtered for type != 'none')
  - existing_themes.json — themes from prior runs (empty list on first run)

Usage:
  prep_opportunities.py <sheet_url> <analyst_merged.json> <classifier_merged.json> <opps_out.json> <themes_out.json>
"""
import json
import re
import sys

import gspread
from google.oauth2.service_account import Credentials

SA_PATH = "/Users/charliejacob/.claude/google-sheets-sa.json"

# Column layout for the Opportunities tab. Keep in sync with master_sheet_writer.py.
OPP_HEADERS = [
    "Theme ID", "Theme name", "Type", "Description",
    "Rule summary", "Suggested change",
    "First seen", "Last seen", "Weeks observed", "Total volume", "Latest week volume",
    "Dominant cluster", "Dominant subcluster",
    "Sample conversations", "Status", "Asana ticket", "Create ticket?",
]


def extract_id(url_or_id):
    m = re.search(r"/d/([a-zA-Z0-9_-]+)", url_or_id)
    return m.group(1) if m else url_or_id


def read_existing_themes(sheet_id):
    creds = Credentials.from_service_account_file(SA_PATH, scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
    ])
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(sheet_id)
    if "Opportunities" not in {ws.title for ws in sh.worksheets()}:
        return []
    ws = sh.worksheet("Opportunities")
    rows = ws.get_all_values()
    if len(rows) < 2:
        return []
    header = rows[0]
    # Map columns by name so layout changes don't silently break us.
    idx = {h: i for i, h in enumerate(header)}
    required = ["Theme ID", "Theme name", "Type", "Description",
                "Rule summary", "Suggested change",
                "Dominant cluster", "Dominant subcluster"]
    for r in required:
        if r not in idx:
            print(f"Opportunities tab missing required column '{r}'", file=sys.stderr)
            return []
    out = []
    for row in rows[1:]:
        if not row or not row[idx["Theme ID"]].strip():
            continue
        out.append({
            "theme_id": row[idx["Theme ID"]].strip(),
            "theme_name": row[idx["Theme name"]].strip(),
            "type": row[idx["Type"]].strip(),
            "description": row[idx["Description"]].strip(),
            "rule_text_summary": row[idx["Rule summary"]].strip(),
            "suggested_change_summary": row[idx["Suggested change"]].strip(),
            "dominant_cluster": row[idx["Dominant cluster"]].strip(),
            "dominant_subcluster": row[idx["Dominant subcluster"]].strip(),
        })
    return out


def main():
    if len(sys.argv) < 6:
        print(f"Usage: {sys.argv[0]} <sheet_url> <analyst.json> <classifier.json> <opps_out.json> <themes_out.json>", file=sys.stderr)
        sys.exit(1)
    sheet_url, an_path, cls_path, opps_out, themes_out = sys.argv[1:6]
    sheet_id = extract_id(sheet_url)

    with open(an_path) as f: analyst = json.load(f)
    with open(cls_path) as f: cls = json.load(f)

    cls_by = {r["index"]: r for r in cls}

    opps = []
    for a in analyst:
        opp = a.get("prompt_opportunity") or {}
        if not isinstance(opp, dict):
            continue
        otype = opp.get("type", "none")
        if otype == "none" or not otype:
            continue
        c = cls_by.get(a["index"], {})
        opps.append({
            "index": a["index"],
            "opportunity_type": otype,
            "rule_text": opp.get("rule_text", ""),
            "suggested_change": opp.get("suggested_change", ""),
            "evidence_quote": opp.get("evidence_quote", ""),
            "cluster": c.get("cluster", "unclassified"),
            "subcluster": c.get("subcluster", "unclassified"),
        })

    themes = read_existing_themes(sheet_id)

    with open(opps_out, "w") as f:
        json.dump(opps, f, indent=2, ensure_ascii=False)
    with open(themes_out, "w") as f:
        json.dump(themes, f, indent=2, ensure_ascii=False)

    stated = sum(1 for o in opps if o["opportunity_type"] == "stated_rule")
    inferred = sum(1 for o in opps if o["opportunity_type"] == "inferred_gap")
    print(f"Wrote {len(opps)} opportunities ({stated} stated_rule, {inferred} inferred_gap) -> {opps_out}")
    print(f"Wrote {len(themes)} existing themes -> {themes_out}")


if __name__ == "__main__":
    main()
