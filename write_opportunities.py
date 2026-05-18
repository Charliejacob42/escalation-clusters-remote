#!/usr/bin/env python3
"""Apply the opportunity-theme merger output to the cumulative Opportunities tab.

Reads:
  - findings.json (current week, has per-conversation opportunity data + indices)
  - theme_merge_output.json (subagent output: per_opportunity decisions + new_themes)
  - Opportunities tab on master sheet (existing themes + user-editable cols)
  - opportunities_state.json sidecar (weekly volumes/samples; created if missing)

Writes:
  - opportunities_state.json (updated weekly_volume + weekly_samples per theme)
  - Opportunities tab on master sheet (full rewrite of theme rows, preserving user edits)

Theme metadata source-of-truth: sheet (user can rename/edit themes inline).
Weekly volume source-of-truth: opportunities_state.json (skill-managed only).
User-editable columns preserved: Status, Asana ticket, Create ticket?.

Usage:
  write_opportunities.py <sheet_url> <week_date_yyyy_mm_dd> <findings.json> <merge_output.json> <state_path>
"""
import json
import os
import re
import sys
from collections import Counter, defaultdict

import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SA_PATH = "/Users/charliejacob/.claude/google-sheets-sa.json"

OPP_HEADERS = [
    "Theme ID", "Theme name", "Type", "Description",
    "Rule summary", "Suggested change",
    "First seen", "Last seen", "Weeks observed", "Total volume", "Latest week volume",
    "Dominant cluster", "Dominant subcluster",
    "Sample conversations", "Status", "Asana ticket", "Create ticket?",
]

NAVY = {"red": 0.12, "green": 0.16, "blue": 0.24}
WHITE = {"red": 1, "green": 1, "blue": 1}
SAMPLE_CONV_CAP = 5  # max sample conversations to render inline


def extract_id(url_or_id):
    m = re.search(r"/d/([a-zA-Z0-9_-]+)", url_or_id)
    return m.group(1) if m else url_or_id


def hyper(url, label):
    if not url:
        return ""
    safe = url.replace('"', '""')
    return f'=HYPERLINK("{safe}", "{label}")'


def load_state(path):
    if not os.path.exists(path):
        return {"version": 1, "themes": {}}
    with open(path) as f:
        return json.load(f)


def save_state(path, state):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def read_existing_sheet(sh):
    """Read existing Opportunities tab. Returns (themes_by_id, user_edits_by_id).
    themes_by_id holds the metadata fields the sheet is source-of-truth for.
    user_edits_by_id holds Status / Asana ticket / Create ticket? per theme_id.
    Both are empty dicts if the tab doesn't exist."""
    titles = {ws.title for ws in sh.worksheets()}
    if "Opportunities" not in titles:
        return {}, {}
    ws = sh.worksheet("Opportunities")
    rows = ws.get_all_values()
    if len(rows) < 2:
        return {}, {}
    header = rows[0]
    idx = {h: i for i, h in enumerate(header)}

    def cell(row, name):
        i = idx.get(name)
        return row[i].strip() if i is not None and i < len(row) else ""

    themes = {}
    edits = {}
    for row in rows[1:]:
        tid = cell(row, "Theme ID")
        if not tid:
            continue
        themes[tid] = {
            "theme_id": tid,
            "theme_name": cell(row, "Theme name"),
            "type": cell(row, "Type"),
            "description": cell(row, "Description"),
            "rule_text_summary": cell(row, "Rule summary"),
            "suggested_change_summary": cell(row, "Suggested change"),
            "dominant_cluster": cell(row, "Dominant cluster"),
            "dominant_subcluster": cell(row, "Dominant subcluster"),
        }
        edits[tid] = {
            "status": cell(row, "Status") or "open",
            "asana_ticket": cell(row, "Asana ticket"),
            "create_ticket": cell(row, "Create ticket?"),
        }
    return themes, edits


def main():
    if len(sys.argv) < 6:
        print(f"Usage: {sys.argv[0]} <sheet_url> <week_date> <findings.json> <merge_output.json> <state_path>", file=sys.stderr)
        sys.exit(1)
    sheet_url, week_date, findings_path, merge_path, state_path = sys.argv[1:6]
    sheet_id = extract_id(sheet_url)

    with open(findings_path) as f: findings = json.load(f)
    with open(merge_path) as f: merge = json.load(f)

    findings_by_idx = {f["index"]: f for f in findings}
    per_opp = merge.get("per_opportunity", [])
    new_themes = merge.get("new_themes", [])

    state = load_state(state_path)
    state.setdefault("themes", {})

    creds = Credentials.from_service_account_file(SA_PATH, scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
    ])
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(sheet_id)
    sheets_api = build("sheets", "v4", credentials=creds)

    sheet_themes, user_edits = read_existing_sheet(sh)

    # Seed state for any new themes from the merger.
    for nt in new_themes:
        tid = nt["theme_id"]
        if tid not in state["themes"]:
            state["themes"][tid] = {
                "weekly_volume": {},
                "weekly_samples": {},
                "first_seen": "",
                "last_seen": "",
            }
        # Also stage the metadata in `sheet_themes` so it renders on first appearance.
        sheet_themes.setdefault(tid, {
            "theme_id": tid,
            "theme_name": nt.get("theme_name", ""),
            "type": nt.get("type", ""),
            "description": nt.get("description", ""),
            "rule_text_summary": nt.get("rule_text_summary", ""),
            "suggested_change_summary": nt.get("suggested_change_summary", ""),
            "dominant_cluster": nt.get("dominant_cluster", ""),
            "dominant_subcluster": nt.get("dominant_subcluster", ""),
        })

    # Re-derive THIS week's per-theme volumes + samples from the merger decisions.
    # Always overwrite the week_date entry (idempotent re-runs).
    week_vol = defaultdict(int)
    week_samples = defaultdict(list)
    week_cluster_votes = defaultdict(Counter)
    week_sub_votes = defaultdict(Counter)
    for po in per_opp:
        if po.get("decision") != "mapped_existing" and po.get("decision") != "new_theme":
            continue
        tid = po.get("theme_id")
        if not tid:
            continue
        idx = po["index"]
        week_vol[tid] += 1
        week_samples[tid].append(idx)
        f = findings_by_idx.get(idx, {})
        if f.get("cluster"):
            week_cluster_votes[tid][f["cluster"]] += 1
        if f.get("subcluster"):
            week_sub_votes[tid][f["subcluster"]] += 1

    for tid, vol in week_vol.items():
        st = state["themes"].setdefault(tid, {"weekly_volume": {}, "weekly_samples": {}, "first_seen": "", "last_seen": ""})
        st["weekly_volume"][week_date] = vol
        st["weekly_samples"][week_date] = week_samples[tid]
        if not st.get("first_seen") or week_date < st["first_seen"]:
            st["first_seen"] = week_date
        if not st.get("last_seen") or week_date > st["last_seen"]:
            st["last_seen"] = week_date

    # For each theme already in state but with no opps THIS week, ensure the week_date
    # entry is removed (handles re-runs where a theme's count drops to zero).
    for tid, st in state["themes"].items():
        if tid not in week_vol and week_date in st.get("weekly_volume", {}):
            del st["weekly_volume"][week_date]
            st.get("weekly_samples", {}).pop(week_date, None)

    save_state(state_path, state)
    print(f"Updated opportunities_state at {state_path} ({len(state['themes'])} themes total)")

    # ---- Render the Opportunities tab ----
    # Themes shown: every theme with non-zero total volume across all weeks.
    titles = {ws.title for ws in sh.worksheets()}
    if "Opportunities" not in titles:
        ws_opp = sh.add_worksheet(title="Opportunities", rows=500, cols=len(OPP_HEADERS))
    else:
        ws_opp = sh.worksheet("Opportunities")
        ws_opp.clear()

    rows_out = [OPP_HEADERS]
    sortable = []
    for tid, st in state["themes"].items():
        weekly = st.get("weekly_volume", {})
        total = sum(weekly.values())
        if total == 0:
            continue
        latest = weekly.get(week_date, 0)
        # Cluster/subcluster: prefer this week's votes; else infer from staged metadata.
        meta = sheet_themes.get(tid, {})
        if week_cluster_votes.get(tid):
            dominant_cluster = week_cluster_votes[tid].most_common(1)[0][0]
        else:
            dominant_cluster = meta.get("dominant_cluster", "")
        if week_sub_votes.get(tid):
            dominant_sub = week_sub_votes[tid].most_common(1)[0][0]
        else:
            dominant_sub = meta.get("dominant_subcluster", "")
        # Sample conversation links: pull from this week's samples first; pad with prior weeks.
        samples = list(week_samples.get(tid, []))
        if len(samples) < SAMPLE_CONV_CAP:
            for w in sorted(st.get("weekly_samples", {}).keys(), reverse=True):
                if w == week_date:
                    continue
                for idx in st["weekly_samples"][w]:
                    if idx in samples:
                        continue
                    samples.append(idx)
                    if len(samples) >= SAMPLE_CONV_CAP:
                        break
                if len(samples) >= SAMPLE_CONV_CAP:
                    break
        # A single Google Sheets cell can only render ONE clickable hyperlink, so
        # we put a plain "#12, #47, ..." index list here. Users cross-reference the
        # Findings tab for the actual conv links.
        sample_idx_strs = [f"#{idx}" for idx in samples[:SAMPLE_CONV_CAP]]
        sample_cell = ", ".join(sample_idx_strs)

        edit = user_edits.get(tid, {})
        rows_out.append([
            tid,
            meta.get("theme_name", ""),
            meta.get("type", ""),
            meta.get("description", ""),
            meta.get("rule_text_summary", ""),
            meta.get("suggested_change_summary", ""),
            st.get("first_seen", ""),
            st.get("last_seen", ""),
            len(weekly),
            total,
            latest,
            dominant_cluster,
            dominant_sub,
            sample_cell,
            edit.get("status", "open"),
            edit.get("asana_ticket", ""),
            (edit.get("create_ticket", "") or "FALSE"),
        ])
        sortable.append((latest, total, len(rows_out) - 1))

    # Sort by latest week volume desc, then total volume desc.
    sortable.sort(key=lambda x: (-x[0], -x[1]))
    body_sorted = [rows_out[0]] + [rows_out[i] for _, _, i in sortable]

    ws_opp.update(values=body_sorted, range_name="A1", value_input_option="USER_ENTERED")

    # Formatting: header row navy, frozen header + first 2 cols, checkbox on Create ticket?.
    sid = ws_opp.id
    n_themes = len(body_sorted) - 1
    requests = [
        {
            "repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1,
                          "startColumnIndex": 0, "endColumnIndex": len(OPP_HEADERS)},
                "cell": {"userEnteredFormat": {
                    "backgroundColor": NAVY,
                    "textFormat": {"foregroundColor": WHITE, "bold": True},
                    "horizontalAlignment": "LEFT",
                    "verticalAlignment": "MIDDLE",
                    "wrapStrategy": "WRAP",
                }},
                "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment,wrapStrategy)",
            }
        },
        {
            "updateSheetProperties": {
                "properties": {"sheetId": sid,
                               "gridProperties": {"frozenRowCount": 1, "frozenColumnCount": 2}},
                "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount",
            }
        },
        {
            "repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": 1, "endRowIndex": max(2, n_themes + 1),
                          "startColumnIndex": 0, "endColumnIndex": len(OPP_HEADERS)},
                "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP", "verticalAlignment": "TOP"}},
                "fields": "userEnteredFormat.wrapStrategy,userEnteredFormat.verticalAlignment",
            }
        },
    ]
    # Column widths.
    widths = [(0, 1, 90), (1, 2, 240), (2, 3, 120), (3, 4, 320),
              (4, 5, 320), (5, 6, 320), (6, 7, 110), (7, 8, 110),
              (8, 9, 100), (9, 10, 100), (10, 11, 110),
              (11, 12, 180), (12, 13, 200), (13, 14, 200),
              (14, 15, 100), (15, 16, 180), (16, 17, 130)]
    for c0, c1, w in widths:
        requests.append({
            "updateDimensionProperties": {
                "range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": c0, "endIndex": c1},
                "properties": {"pixelSize": w},
                "fields": "pixelSize",
            }
        })
    # Checkbox validation on the Create ticket? column.
    ct_col_idx = OPP_HEADERS.index("Create ticket?")
    if n_themes > 0:
        requests.append({
            "setDataValidation": {
                "range": {"sheetId": sid, "startRowIndex": 1, "endRowIndex": n_themes + 1,
                          "startColumnIndex": ct_col_idx, "endColumnIndex": ct_col_idx + 1},
                "rule": {"condition": {"type": "BOOLEAN"}, "strict": True, "showCustomUi": True},
            }
        })
    sheets_api.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body={"requests": requests}).execute()

    print(f"Wrote Opportunities tab: {n_themes} themes (latest week: {week_date})")


if __name__ == "__main__":
    main()
