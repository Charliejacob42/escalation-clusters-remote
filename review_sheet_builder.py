#!/usr/bin/env python3
"""Build a per-week review tab in the master sheet from toggled-on subclusters.

Reads:
  - Review queue tab: subclusters with column D (Build review tab) checked
  - Latest run findings tab: per-conversation findings from the most recent run

Writes:
  - New tab "Review YYYY-MM-DD" (today's date in ET); overwrites if it exists
  - Auto-populated: Propelix, Front, CRM, Display name, Cluster, Subcluster,
    User intent, Where bot got stuck, What agent did
  - Reviewer-fill: Carrier, How did agent solve?, Self-serve opportunity, Tags (dropdown), Notes

Usage: review_sheet_builder.py <sheet_url>
"""
import datetime as dt
import re
import sys
from zoneinfo import ZoneInfo

import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SA_PATH = "/Users/charliejacob/.claude/google-sheets-sa.json"
TAGS_DROPDOWN = [
    "knowledge", "carrier_site", "crm", "request_not_possible",
    "carrier_callout", "human_preferred", "other", "legal",
    "reconnecting_to_agent", "app_issues",
]


def extract_id(url_or_id):
    m = re.search(r"/d/([a-zA-Z0-9_-]+)", url_or_id)
    return m.group(1) if m else url_or_id


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <sheet_url>", file=sys.stderr)
        sys.exit(1)
    sheet_url = sys.argv[1]
    sheet_id = extract_id(sheet_url)

    creds = Credentials.from_service_account_file(SA_PATH, scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
    ])
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(sheet_id)
    sheets_api = build("sheets", "v4", credentials=creds)
    print(f"Opened: {sh.title}")

    # Read Review queue with UNFORMATTED so checkboxes come back as bool
    rq_resp = sheets_api.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range="'Review queue'!A1:D200",
        valueRenderOption="UNFORMATTED_VALUE",
    ).execute()
    rq_rows = rq_resp.get("values", [])
    if len(rq_rows) < 2:
        print("Review queue tab is empty or missing rows.", file=sys.stderr)
        sys.exit(1)

    toggled = []  # list of (cluster, subcluster)
    for row in rq_rows[1:]:
        if len(row) < 4:
            continue
        cluster, sub, _fix, toggle = row[0], row[1], row[2], row[3]
        if toggle is True:
            toggled.append((cluster, sub))

    if not toggled:
        print("No subclusters toggled on. Flip checkboxes in the Review queue tab and re-run.")
        sys.exit(0)

    print(f"Toggled: {len(toggled)} subclusters")
    for c, s in toggled:
        print(f"  - {c} / {s}")

    # Read Latest run findings via FORMULA to preserve HYPERLINK formulas
    lrf_resp = sheets_api.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range="'Latest run findings'!A1:U10000",
        valueRenderOption="FORMULA",
    ).execute()
    lrf_rows = lrf_resp.get("values", [])
    if len(lrf_rows) < 2:
        print("Latest run findings tab is empty. Run a weekly run first.", file=sys.stderr)
        sys.exit(1)

    headers = lrf_rows[0]
    col = {h: i for i, h in enumerate(headers)}
    required = ["Display name", "Propelix", "Front", "CRM", "Cluster", "Subcluster",
                "User intent", "Where bot got stuck", "What agent did"]
    for r in required:
        if r not in col:
            print(f"Missing column in Latest run findings: {r}", file=sys.stderr)
            sys.exit(1)

    toggled_set = set(toggled)
    matching = []
    for row in lrf_rows[1:]:
        if len(row) <= col["Subcluster"]:
            continue
        cluster = row[col["Cluster"]]
        sub = row[col["Subcluster"]]
        if (cluster, sub) in toggled_set:
            matching.append(row)

    if not matching:
        print("No conversations match the toggled subclusters in the latest run.")
        sys.exit(0)
    print(f"Matching conversations: {len(matching)}")

    review_headers = [
        "Propelix", "Front", "CRM", "Display name", "Cluster", "Subcluster",
        "User intent", "Where bot got stuck", "What agent did",
        "Carrier", "How did agent solve?", "Self-serve opportunity", "Tags", "Notes",
    ]
    review_rows = [review_headers]

    def cell(row, header):
        idx = col[header]
        return row[idx] if len(row) > idx else ""

    for row in matching:
        review_rows.append([
            cell(row, "Propelix"),
            cell(row, "Front"),
            cell(row, "CRM"),
            cell(row, "Display name"),
            cell(row, "Cluster"),
            cell(row, "Subcluster"),
            cell(row, "User intent"),
            cell(row, "Where bot got stuck"),
            cell(row, "What agent did"),
            "", "", "", "", "",
        ])

    today = dt.datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    tab_name = f"Review {today}"

    existing = {ws.title: ws for ws in sh.worksheets()}
    if tab_name in existing:
        review_ws = existing[tab_name]
        review_ws.clear()
        print(f"Overwriting existing tab: {tab_name}")
    else:
        review_ws = sh.add_worksheet(title=tab_name,
                                     rows=max(200, len(review_rows) + 50),
                                     cols=20)
        print(f"Created tab: {tab_name}")

    review_ws.update(values=review_rows, range_name="A1",
                     value_input_option="USER_ENTERED")
    print(f"Wrote {len(review_rows) - 1} conversation rows")

    review_sid = review_ws.id
    requests = []

    # Header row
    requests.append({
        "repeatCell": {
            "range": {"sheetId": review_sid, "startRowIndex": 0, "endRowIndex": 1,
                      "startColumnIndex": 0, "endColumnIndex": len(review_headers)},
            "cell": {"userEnteredFormat": {
                "backgroundColor": {"red": 0.12, "green": 0.16, "blue": 0.24},
                "textFormat": {"foregroundColor": {"red": 1, "green": 1, "blue": 1}, "bold": True},
                "horizontalAlignment": "LEFT",
                "verticalAlignment": "MIDDLE",
                "wrapStrategy": "WRAP",
            }},
            "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment,wrapStrategy)",
        }
    })

    # Freeze header
    requests.append({
        "updateSheetProperties": {
            "properties": {"sheetId": review_sid,
                           "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount",
        }
    })

    # Wrap text on long columns
    for c_idx in [3, 4, 5, 6, 7, 8, 10, 11, 13]:
        requests.append({
            "repeatCell": {
                "range": {"sheetId": review_sid, "startRowIndex": 1,
                          "startColumnIndex": c_idx, "endColumnIndex": c_idx + 1},
                "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP", "verticalAlignment": "TOP"}},
                "fields": "userEnteredFormat.wrapStrategy,userEnteredFormat.verticalAlignment",
            }
        })

    # Tags dropdown on column M (index 12)
    requests.append({
        "setDataValidation": {
            "range": {"sheetId": review_sid, "startRowIndex": 1,
                      "endRowIndex": len(review_rows),
                      "startColumnIndex": 12, "endColumnIndex": 13},
            "rule": {
                "condition": {
                    "type": "ONE_OF_LIST",
                    "values": [{"userEnteredValue": t} for t in TAGS_DROPDOWN],
                },
                "strict": True,
                "showCustomUi": True,
            },
        }
    })

    width_specs = [
        (0, 1, 100), (1, 2, 100), (2, 3, 100),
        (3, 4, 160), (4, 5, 200), (5, 6, 220),
        (6, 7, 320), (7, 8, 320), (8, 9, 320),
        (9, 10, 140), (10, 11, 280), (11, 12, 200),
        (12, 13, 160), (13, 14, 280),
    ]
    for c0, c1, w in width_specs:
        requests.append({
            "updateDimensionProperties": {
                "range": {"sheetId": review_sid, "dimension": "COLUMNS",
                          "startIndex": c0, "endIndex": c1},
                "properties": {"pixelSize": w},
                "fields": "pixelSize",
            }
        })

    sheets_api.spreadsheets().batchUpdate(spreadsheetId=sheet_id,
                                          body={"requests": requests}).execute()
    print(f"Applied {len(requests)} formatting requests")

    print(f"\nDone. Sheet: https://docs.google.com/spreadsheets/d/{sheet_id}/edit#gid={review_sid}")


if __name__ == "__main__":
    main()
