#!/usr/bin/env python3
"""Append a week's run to the v4 master tracker sheet.

Tabs touched per run:
  - Tracker: appends 1 col per week to BOTH grids (top: uplift; bottom: combined "<count> (<share>%)")
            and to header band (rows 1-5).
  - Review queue: refreshes metrics + re-sorts by latest week's uplift desc, preserving toggle state
            (existing toggle for a (cluster, subcluster) pair carries over to its new row position).
  - Creates "Findings YYYY-MM-DD" with per-conversation findings.
  - Creates "Taxonomy output YYYY-MM-DD" with cluster + subcluster snapshot for the week.
  - Creates separator tab "Week of YYYY-MM-DD" (gray-tinted).
  - Reorders tabs: Tracker, Review queue, Taxonomy, then per-week groups newest-first.

Usage: master_sheet_writer.py <sheet_url> <week_date_yyyy_mm_dd> <findings.json> <cluster_summary.json> <subcluster_summary.json> <ss_result.json>
"""
import json
import re
import sys

import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SA_PATH = "/Users/charliejacob/.claude/google-sheets-sa.json"

FINDINGS_HEADERS = [
    "#", "Display name", "Propelix", "Front", "CRM", "Escalation summary",
    "Cluster", "Subcluster", "Confidence", "Complexity",
    "Escalation type", "Team", "Channel",
    "User stage", "Is PH", "Escalation time",
    "User intent", "Where bot got stuck", "What agent did",
    "Classifier rationale", "Fix signature",
    "Opp type", "Opp rule", "Opp suggested change", "Opp evidence", "Opp theme ID",
]

NAVY = {"red": 0.12, "green": 0.16, "blue": 0.24}
WHITE = {"red": 1, "green": 1, "blue": 1}
CLUSTER_TINT = {"red": 0.93, "green": 0.95, "blue": 0.98}
SEPARATOR_TINT = {"red": 0.7, "green": 0.7, "blue": 0.7}


def extract_id(url_or_id):
    m = re.search(r"/d/([a-zA-Z0-9_-]+)", url_or_id)
    return m.group(1) if m else url_or_id


def hyper(url, label):
    if not url or url in ("No Conversation ID", "No Front Link", "Phone Escalation"):
        return url or ""
    safe = url.replace('"', '""')
    return f'=HYPERLINK("{safe}", "{label}")'


def col_letter(idx):
    s, n = "", idx
    while True:
        s = chr(ord("A") + n % 26) + s
        n = n // 26 - 1
        if n < 0:
            break
    return s


def fmt_count_share(records, share):
    return f"{int(records)} ({share * 100:.1f}%)"


def insert_new_tracker_rows(
    sheets_api, sheet_id, tracker_sheet_id,
    top_keys, bottom_keys,
    top_grid_header_idx, bottom_grid_header_idx,
    last_row_idx,
    cluster_records, sub_records,
):
    """Detect clusters and (cluster, subcluster) pairs in the input data that don't exist
    in top_keys / bottom_keys yet, then insert blank rows into BOTH grids of the Tracker
    sheet at the right positions. Prior-week cells in inserted rows are left blank.

    Returns: (top_keys, bottom_keys, bottom_grid_header_idx, last_row_idx, missing_pairs)
    where the keys lists and indices reflect the post-insertion sheet state, and
    missing_pairs is the list of (cluster, subcluster) pairs that triggered insertions
    (empty subcluster string for bare-cluster insertions).
    """
    existing_clusters_top = {c for (c, s, _) in top_keys if s == ""}
    existing_pairs_top = {(c, s) for (c, s, _) in top_keys if s != ""}
    existing_clusters_bot = {c for (c, s, _) in bottom_keys if s == ""}
    existing_pairs_bot = {(c, s) for (c, s, _) in bottom_keys if s != ""}

    # Sanity: top and bottom grids should mirror each other. If not, something has
    # already drifted; bail with a clear message rather than silently misaligning.
    if existing_clusters_top != existing_clusters_bot or existing_pairs_top != existing_pairs_bot:
        only_top_c = existing_clusters_top - existing_clusters_bot
        only_bot_c = existing_clusters_bot - existing_clusters_top
        only_top_s = existing_pairs_top - existing_pairs_bot
        only_bot_s = existing_pairs_bot - existing_pairs_top
        print(
            "Tracker top and bottom grids are out of sync; refusing to auto-insert rows. "
            f"Top-only clusters: {only_top_c}; Bottom-only clusters: {only_bot_c}; "
            f"Top-only subs: {only_top_s}; Bottom-only subs: {only_bot_s}",
            file=sys.stderr,
        )
        sys.exit(1)

    # Identify what's missing.
    input_clusters = set(cluster_records.keys())
    input_pairs = set(sub_records.keys())  # (cluster, subcluster) pairs from sub_summary

    missing_clusters = sorted(input_clusters - existing_clusters_top)
    missing_pairs = sorted([(c, s) for (c, s) in input_pairs if (c, s) not in existing_pairs_top])

    if not missing_clusters and not missing_pairs:
        return top_keys, bottom_keys, bottom_grid_header_idx, last_row_idx, []

    # Build a pretty list of all newly inserted (cluster, subcluster) tuples for the warning.
    # Bare new clusters show up as (cluster, "").
    warn_pairs = [(c, "") for c in missing_clusters] + missing_pairs
    print(
        "WARNING: inserting new Tracker rows for (cluster, subcluster) pairs not seen in prior weeks: "
        f"{warn_pairs}",
        file=sys.stderr,
    )

    # ----- Plan top-grid insertions -----
    # For each new cluster, place its cluster row immediately before the first existing
    # "unclassified" cluster row if present; otherwise at the very bottom of the top grid
    # (i.e., right before bottom_grid_header_idx, leaving the 2 blank rows untouched).
    # For each missing subcluster, place its row immediately after the last existing
    # subcluster of its parent cluster (or right after the cluster row if no subs yet).
    # Multiple new subclusters under the same cluster are inserted in alphabetical order.

    def find_top_cluster_anchor_for_new_cluster():
        # Returns the original-frame row index where a new cluster's first row goes.
        # Prefer "right before the unclassified cluster row" if it exists; else the row
        # right after the last existing top-grid row (i.e., one of the trailing blank rows).
        unclass_row = None
        for c, s, ri in top_keys:
            if s == "" and c == "unclassified":
                unclass_row = ri
                break
        if unclass_row is not None:
            return unclass_row
        # No unclassified — find the row right after the last top-grid row.
        if top_keys:
            return top_keys[-1][2] + 1
        return top_grid_header_idx + 1

    def find_top_sub_anchor(parent_cluster):
        # Returns the original-frame row index where a new subcluster row goes (under
        # its parent cluster). The parent cluster MUST already exist in top_keys.
        parent_row = None
        last_sub_row = None
        for c, s, ri in top_keys:
            if c == parent_cluster and s == "":
                parent_row = ri
                last_sub_row = ri  # default: just after the cluster row if no subs
            elif c == parent_cluster and s != "" and parent_row is not None:
                last_sub_row = ri
        if parent_row is None:
            return None
        return last_sub_row + 1

    def find_bot_cluster_anchor_for_new_cluster():
        unclass_row = None
        for c, s, ri in bottom_keys:
            if s == "" and c == "unclassified":
                unclass_row = ri
                break
        if unclass_row is not None:
            return unclass_row
        if bottom_keys:
            return bottom_keys[-1][2] + 1
        return bottom_grid_header_idx + 1

    def find_bot_sub_anchor(parent_cluster):
        parent_row = None
        last_sub_row = None
        for c, s, ri in bottom_keys:
            if c == parent_cluster and s == "":
                parent_row = ri
                last_sub_row = ri
            elif c == parent_cluster and s != "" and parent_row is not None:
                last_sub_row = ri
        if parent_row is None:
            return None
        return last_sub_row + 1

    # Build per-cluster subcluster groupings (deterministic alphabetical order).
    # If a missing pair's parent cluster is not in existing_clusters_top AND not in
    # missing_clusters (i.e., orphan parent), promote it into missing_clusters so the
    # cluster row gets inserted alongside its subs.
    missing_clusters_set = set(missing_clusters)
    for c, _ in missing_pairs:
        if c not in existing_clusters_top and c not in missing_clusters_set:
            missing_clusters_set.add(c)
            missing_clusters.append(c)
    missing_clusters.sort()

    new_subs_by_existing_parent = {}
    new_subs_by_new_parent = {}
    for c, s in missing_pairs:
        if c in existing_clusters_top:
            new_subs_by_existing_parent.setdefault(c, []).append(s)
        else:
            new_subs_by_new_parent.setdefault(c, []).append(s)
    for d in (new_subs_by_existing_parent, new_subs_by_new_parent):
        for c in d:
            d[c].sort()

    # Plan list: each entry is (original_row_idx, group_id, local_offset, parent_cluster,
    # sub_or_blank, grid_tag). parent_cluster is the cluster name (for both cluster rows
    # and subcluster rows under that cluster); sub_or_blank is "" for cluster rows.
    # local_offset orders multiple insertions sharing the same anchor; group_id is purely
    # a tiebreaker for stable sorting. grid_tag is "top" or "bot".
    plan = []
    group_id = 0

    # New existing-parent subclusters in the top grid.
    for parent in sorted(new_subs_by_existing_parent.keys()):
        anchor = find_top_sub_anchor(parent)
        if anchor is None:
            continue
        for offset, sub in enumerate(new_subs_by_existing_parent[parent]):
            plan.append((anchor, group_id, offset, parent, sub, "top"))
        group_id += 1

    # New existing-parent subclusters in the bottom grid.
    for parent in sorted(new_subs_by_existing_parent.keys()):
        anchor = find_bot_sub_anchor(parent)
        if anchor is None:
            continue
        for offset, sub in enumerate(new_subs_by_existing_parent[parent]):
            plan.append((anchor, group_id, offset, parent, sub, "bot"))
        group_id += 1

    # Brand-new clusters (cluster row + any subclusters), top grid then bottom grid.
    # Multiple new clusters in the same run go in alphabetical order at the same anchor;
    # within each cluster, the cluster row comes first, then its subs in alphabetical order.
    for grid_tag, anchor_fn in (("top", find_top_cluster_anchor_for_new_cluster),
                                 ("bot", find_bot_cluster_anchor_for_new_cluster)):
        anchor = anchor_fn()
        offset = 0
        for c in missing_clusters:
            plan.append((anchor, group_id, offset, c, "", grid_tag))
            offset += 1
            for s in new_subs_by_new_parent.get(c, []):
                plan.append((anchor, group_id, offset, c, s, grid_tag))
                offset += 1
        group_id += 1

    # Compute final post-insertion row indices for each plan entry.
    # final_pos = original_pos + (count of insertions with original_pos < this) + local_offset
    # For ties on original_pos, the entry with the smallest local_offset ends up at the
    # smallest final_pos within the group. Across groups sharing the same anchor, group
    # order determines which group sits first. We sort the plan by
    # (original_row_idx, group_id, local_offset) so that earlier entries get smaller
    # final positions.
    plan.sort(key=lambda x: (x[0], x[1], x[2]))

    # Walk plan in order and assign final positions sequentially.
    final_positions = []  # parallel to plan
    cumulative_inserts_before = 0
    last_anchor = None
    anchor_group_offset = 0
    for entry in plan:
        original_row, _gid, _lo, _parent, _sub, _grid = entry
        if original_row != last_anchor:
            cumulative_inserts_before += anchor_group_offset
            last_anchor = original_row
            anchor_group_offset = 0
        final_pos = original_row + cumulative_inserts_before + anchor_group_offset
        final_positions.append(final_pos)
        anchor_group_offset += 1

    # ----- Submit the insertion + cell-write batchUpdate -----
    # Process insertDimension requests in DESCENDING order of final_pos so the
    # original-frame index of each insertion is still valid when it's processed
    # (later insertions at higher final positions have already been applied and don't
    # affect rows above them; earlier insertions at lower final positions haven't been
    # applied yet, so they don't shift this one).
    requests = []
    insert_order = sorted(range(len(plan)), key=lambda i: -final_positions[i])
    for i in insert_order:
        original_row = plan[i][0]
        requests.append({
            "insertDimension": {
                "range": {
                    "sheetId": tracker_sheet_id,
                    "dimension": "ROWS",
                    "startIndex": original_row,
                    "endIndex": original_row + 1,
                },
                "inheritFromBefore": False,
            }
        })

    # Write col A and col B at each new row's FINAL position.
    for i, entry in enumerate(plan):
        _, _, _, parent, sub_name, _ = entry
        col_a = parent if sub_name == "" else ""
        col_b = sub_name
        fp = final_positions[i]
        requests.append({
            "updateCells": {
                "range": {
                    "sheetId": tracker_sheet_id,
                    "startRowIndex": fp,
                    "endRowIndex": fp + 1,
                    "startColumnIndex": 0,
                    "endColumnIndex": 2,
                },
                "rows": [{"values": [
                    {"userEnteredValue": {"stringValue": col_a}},
                    {"userEnteredValue": {"stringValue": col_b}},
                ]}],
                "fields": "userEnteredValue",
            }
        })

    sheets_api.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id, body={"requests": requests}
    ).execute()
    print(f"Tracker: inserted {len(plan)} new rows across both grids "
          f"({len([e for e in plan if e[5] == 'top'])} top, "
          f"{len([e for e in plan if e[5] == 'bot'])} bottom)")

    # ----- Update top_keys, bottom_keys, bottom_grid_header_idx in memory -----
    # Shift every existing key by the number of insertions at final_pos <= its old idx.
    # Then add the newly inserted entries at their final_pos.
    all_inserts = sorted(final_positions)

    def shift(ri):
        # Every insertion at final_pos <= ri shifts ri down by 1. (final_pos == ri means
        # the new row took ri's slot, pushing ri to ri+1.)
        return ri + sum(1 for p in all_inserts if p <= ri)

    new_top_keys = [(c, s, shift(ri)) for (c, s, ri) in top_keys]
    new_bottom_keys = [(c, s, shift(ri)) for (c, s, ri) in bottom_keys]
    for i, entry in enumerate(plan):
        _, _, _, parent, sub_name, grid_tag = entry
        key = (parent, sub_name, final_positions[i])
        if grid_tag == "top":
            new_top_keys.append(key)
        else:
            new_bottom_keys.append(key)
    new_top_keys.sort(key=lambda k: k[2])
    new_bottom_keys.sort(key=lambda k: k[2])

    new_bottom_grid_header_idx = shift(bottom_grid_header_idx)
    new_last_row_idx = last_row_idx + len(plan)

    return new_top_keys, new_bottom_keys, new_bottom_grid_header_idx, new_last_row_idx, warn_pairs


def main():
    if len(sys.argv) < 7:
        print(f"Usage: {sys.argv[0]} <sheet_url> <week_date_yyyy_mm_dd> <findings.json> <cluster_summary.json> <subcluster_summary.json> <ss_result.json>", file=sys.stderr)
        sys.exit(1)
    sheet_url, week_date, findings_path, cluster_path, sub_path, ss_path = sys.argv[1:7]
    sheet_id = extract_id(sheet_url)
    week_label = f"Week of {week_date}"

    findings = json.load(open(findings_path))
    cluster_summary = json.load(open(cluster_path))
    sub_summary = json.load(open(sub_path))
    ss_result = json.load(open(ss_path))
    if isinstance(ss_result, list):
        ss_result = ss_result[0]

    total_bot = int(ss_result["total_bot_convs"])
    escalated = int(ss_result["escalated_convs"])
    ss_rate = float(ss_result["self_serve_rate"])
    esc_rate = float(ss_result["escalation_rate"])

    cluster_records = {c["cluster"]: int(c["records"]) for c in cluster_summary}
    cluster_share = {c["cluster"]: float(c["share_of_sample"]) for c in cluster_summary}
    cluster_uplift = {c: cluster_share[c] * esc_rate for c in cluster_share}

    sub_records = {(s["cluster"], s["subcluster"]): int(s["records"]) for s in sub_summary}
    sub_share = {(s["cluster"], s["subcluster"]): float(s["share_of_sample"]) for s in sub_summary}
    sub_uplift = {key: sub_share[key] * esc_rate for key in sub_share}
    sub_fix = {(s["cluster"], s["subcluster"]): s.get("fix_signature", "") for s in sub_summary}

    creds = Credentials.from_service_account_file(SA_PATH, scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
    ])
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(sheet_id)
    sheets_api = build("sheets", "v4", credentials=creds)
    print(f"Opened: {sh.title}")

    ws_ids = {ws.title: ws.id for ws in sh.worksheets()}
    if "Tracker" not in ws_ids:
        print("Tracker tab missing. Run rebuild_v5.py first.", file=sys.stderr)
        sys.exit(1)

    # ============================================================
    # 1. APPEND WEEK COL TO TRACKER (top grid + bottom grid + band)
    # ============================================================
    tracker_ws = sh.worksheet("Tracker")
    rows = tracker_ws.get_all_values()

    # Find both grid headers (rows where col A == "Cluster")
    grid_header_rows = [i for i, r in enumerate(rows) if r and r[0] == "Cluster"]
    if len(grid_header_rows) < 2:
        print(f"Tracker structure unexpected: found {len(grid_header_rows)} grid header rows", file=sys.stderr)
        sys.exit(1)
    top_grid_header_idx = grid_header_rows[0]      # 0-indexed
    bottom_grid_header_idx = grid_header_rows[1]   # 0-indexed

    # Detect an existing "Trailing N-wk avg" column. It always lives one column to the
    # right of the most-recent week column. Identified by the top grid header containing
    # a string that begins with "Trailing".
    top_header_row = rows[top_grid_header_idx]
    existing_trailing_col = None
    for ci in range(len(top_header_row) - 1, -1, -1):
        cell_val = top_header_row[ci] if ci < len(top_header_row) else ""
        if cell_val.startswith("Trailing"):
            existing_trailing_col = ci
            break

    # Determine target col for the new week column.
    # - Re-running a week (week_label already in band row 0): use the existing column.
    # - First-time append with an existing trailing-avg col: take its position (it shifts right).
    # - First-time append with no trailing-avg col yet: append after the last filled col.
    band_row = rows[0] if rows else []
    target_col_idx = None
    for ci, val in enumerate(band_row):
        if val == week_label:
            target_col_idx = ci
            break
    if target_col_idx is None:
        if existing_trailing_col is not None:
            target_col_idx = existing_trailing_col
        else:
            target_col_idx = len(top_header_row)
    next_col_idx = target_col_idx

    # Trailing-avg column always lands one column to the right of the new week col,
    # unless we're re-running a past week (then leave it where it is).
    if target_col_idx == existing_trailing_col or existing_trailing_col is None:
        trailing_col_idx = next_col_idx + 1
    else:
        # Re-run of a past week. existing_trailing_col stays put.
        trailing_col_idx = existing_trailing_col

    # Build (cluster, sub, row_idx_0based) for both grids.
    # Subcluster rows have col A empty; track the most recent cluster header to attach the parent.
    top_keys = []
    current_cluster = ""
    for i in range(top_grid_header_idx + 1, bottom_grid_header_idx):
        r = rows[i]
        if not r or not any(r):
            continue
        a = r[0] if len(r) > 0 else ""
        b = r[1] if len(r) > 1 else ""
        if a and not b:
            current_cluster = a
            top_keys.append((a, "", i))
        elif b:
            top_keys.append((current_cluster, b, i))

    bottom_keys = []
    current_cluster = ""
    for i in range(bottom_grid_header_idx + 1, len(rows)):
        r = rows[i]
        if not r or not any(r):
            continue
        a = r[0] if len(r) > 0 else ""
        b = r[1] if len(r) > 1 else ""
        if a and not b:
            current_cluster = a
            bottom_keys.append((a, "", i))
        elif b:
            bottom_keys.append((current_cluster, b, i))

    # Insert any (cluster, subcluster) rows that are present in this week's input but not
    # in the existing Tracker grids. New rows go into BOTH grids with prior-week cells blank,
    # then top_keys / bottom_keys / bottom_grid_header_idx / last_row_idx are refreshed so
    # the new-column population loop below picks them up.
    initial_last_row_idx = max(bottom_keys[-1][2] if bottom_keys else 0, len(rows) - 1)
    top_keys, bottom_keys, bottom_grid_header_idx, last_row_idx, _ = insert_new_tracker_rows(
        sheets_api, sheet_id, ws_ids["Tracker"],
        top_keys, bottom_keys,
        top_grid_header_idx, bottom_grid_header_idx,
        initial_last_row_idx,
        cluster_records, sub_records,
    )

    # Build the new column values across the full (post-insertion) sheet height
    new_col = [""] * (last_row_idx + 1)

    # Header band rows 0-4 (0-indexed)
    new_col[0] = week_label
    new_col[1] = total_bot
    new_col[2] = escalated
    new_col[3] = esc_rate
    new_col[4] = ss_rate

    # Top grid header
    new_col[top_grid_header_idx] = f"Uplift {week_label}"

    # Top grid data
    for c, s, ri in top_keys:
        if s == "":
            new_col[ri] = cluster_uplift.get(c, 0)
        else:
            new_col[ri] = sub_uplift.get((c, s), 0)

    # Bottom grid header
    new_col[bottom_grid_header_idx] = f"Counts {week_label}"

    # Bottom grid data
    for c, s, ri in bottom_keys:
        if s == "":
            new_col[ri] = fmt_count_share(cluster_records.get(c, 0), cluster_share.get(c, 0))
        else:
            new_col[ri] = fmt_count_share(sub_records.get((c, s), 0), sub_share.get((c, s), 0))

    cl = col_letter(next_col_idx)
    tracker_ws.update(values=[[v] for v in new_col],
                      range_name=f"{cl}1:{cl}{len(new_col)}",
                      value_input_option="USER_ENTERED")
    print(f"Tracker: appended col {cl} ({week_label})")

    # ----------------------------------------------------------------
    # 1b. TRAILING N-WEEK AVERAGE COLUMN
    # ----------------------------------------------------------------
    # Toggle lives in B5 (numeric). A5 holds the label. Formulas use
    # OFFSET(<this_cell>, 0, -$B$6, 1, $B$6) so changing B5 instantly
    # re-averages the prior N week columns. AVERAGE() ignores blanks,
    # so subclusters with <N weeks of history average over what exists.
    trailing_cl = col_letter(trailing_col_idx)
    trailing_col_values = [""] * (last_row_idx + 1)

    def trailing_formula(row_1indexed):
        cell_ref = f"{trailing_cl}{row_1indexed}"
        return f'=IFERROR(AVERAGE(OFFSET({cell_ref},0,-$B$6,1,$B$6)),"")'

    # Band header label (row 0) and band metric trailing avgs (rows 1-4)
    trailing_col_values[0] = '="Trailing "&$B$6&"-wk avg"'
    for r in range(1, 5):
        trailing_col_values[r] = trailing_formula(r + 1)
    # Top grid header label (dynamic, matches band)
    trailing_col_values[top_grid_header_idx] = '="Trailing "&$B$6&"-wk avg"'
    # Top grid data: one formula per cluster/subcluster row
    for c, s, ri in top_keys:
        trailing_col_values[ri] = trailing_formula(ri + 1)

    tracker_ws.update(values=[[v] for v in trailing_col_values],
                      range_name=f"{trailing_cl}1:{trailing_cl}{len(trailing_col_values)}",
                      value_input_option="USER_ENTERED")
    print(f"Tracker: wrote trailing-avg col {trailing_cl}")

    # Toggle cell. Preserve user-edited B6 if it's already numeric; otherwise default to 3.
    # The toggle lives in row 6 (cell-ref), which is the blank row between the band
    # metrics (rows 1-5: header, total convs, escalations, esc rate, ss rate) and the
    # top grid header (row 7).
    current_b6_raw = ""
    try:
        current_b6_raw = (tracker_ws.acell("B6").value or "").strip()
    except Exception:
        pass
    try:
        float(current_b6_raw)
        b6_value = current_b6_raw
    except ValueError:
        b6_value = "3"
    tracker_ws.update(values=[["Trailing avg weeks:", b6_value]],
                      range_name="A6:B6",
                      value_input_option="USER_ENTERED")

    # ============================================================
    # 2. CREATE FINDINGS YYYY-MM-DD TAB (Review queue removed in v5)
    # ============================================================
    findings_title = f"Findings {week_date}"
    ws_ids = {ws.title: ws.id for ws in sh.worksheets()}
    if findings_title in ws_ids:
        f_ws = sh.worksheet(findings_title)
        f_ws.clear()
        print(f"Cleared existing tab: {findings_title}")
    else:
        f_ws = sh.add_worksheet(title=findings_title,
                                rows=max(550, len(findings) + 50), cols=len(FINDINGS_HEADERS))
        print(f"Created tab: {findings_title}")

    f_rows = [FINDINGS_HEADERS]
    for f in findings:
        f_rows.append([
            f["index"], f["display_name"],
            hyper(f["propelix_link"], "Propelix"),
            hyper(f["front_link"], "Front"),
            hyper(f["crm_link"], "CRM"),
            f["escalation_summary"],
            f["cluster"], f["subcluster"],
            f.get("classifier_confidence", ""),
            f["complexity"],
            f["escalation_type"], f["team"], f["channel"],
            f.get("user_stage_label") or "",
            "Yes" if f.get("is_policyholder") else "No",
            f["escalation_timestamp"],
            f["user_intent"], f["where_bot_got_stuck"], f["what_agent_did"],
            f.get("classifier_rationale", ""),
            f["fix_signature"],
            f.get("opportunity_type", "none"),
            f.get("opportunity_rule_text", ""),
            f.get("opportunity_suggested_change", ""),
            f.get("opportunity_evidence_quote", ""),
            f.get("opportunity_theme_id", ""),
        ])
    f_ws.update(values=f_rows, range_name="A1", value_input_option="USER_ENTERED")
    print(f"  wrote {len(f_rows) - 1} conversation rows")

    # ============================================================
    # 4. CREATE TAXONOMY OUTPUT YYYY-MM-DD TAB
    # ============================================================
    tax_title = f"Taxonomy output {week_date}"
    ws_ids = {ws.title: ws.id for ws in sh.worksheets()}

    # Preserve existing toggles before recreating, if tab exists
    existing_toggles = {}
    string_toggle_warned = False
    if tax_title in ws_ids:
        existing_resp = sheets_api.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=f"'{tax_title}'!A2:G500",
            valueRenderOption="UNFORMATTED_VALUE",
        ).execute()
        for row_idx, row in enumerate(existing_resp.get("values", []), start=2):
            if len(row) >= 7 and row[1] and (
                row[6] is True
                or (isinstance(row[6], str) and row[6].strip().upper() == "TRUE")
            ):
                if isinstance(row[6], str) and not string_toggle_warned:
                    print(
                        f"WARNING: found text-typed toggle on row {row_idx} "
                        f"(cluster={row[0]}, sub={row[1]}); preserving but expected boolean checkbox",
                        file=sys.stderr,
                    )
                    string_toggle_warned = True
                existing_toggles[(row[0] if row[0] else "", row[1])] = True
        sh.del_worksheet(sh.worksheet(tax_title))
    tax_ws = sh.add_worksheet(title=tax_title, rows=200, cols=10)

    tax_rows = [["Cluster", "Subcluster", "# conversations", "% of sample",
                 "Self-serve uplift est.", "Fix signature", "Build review tab"]]

    # Order by week's uplift desc (cluster level), within each cluster by uplift desc
    cluster_order = sorted(cluster_uplift.keys(), key=lambda c: -cluster_uplift.get(c, 0))

    sub_by_cluster = {}
    for (c, s) in sub_uplift.keys():
        sub_by_cluster.setdefault(c, []).append(s)
    for c in sub_by_cluster:
        sub_by_cluster[c].sort(key=lambda s: -sub_uplift.get((c, s), 0))

    tax_cluster_row_idxs = []
    tax_subcluster_row_idxs = []  # rows that should get a checkbox
    for c in cluster_order:
        # Walk to find the cluster name as it appears in toggles (could be empty for subclusters)
        tax_cluster_row_idxs.append(len(tax_rows))  # 0-indexed in tax_rows
        tax_rows.append([
            c, "",
            cluster_records.get(c, 0),
            cluster_share.get(c, 0),
            cluster_uplift.get(c, 0),
            "",
            "",
        ])
        for s in sub_by_cluster.get(c, []):
            tax_subcluster_row_idxs.append(len(tax_rows))
            toggle = existing_toggles.get((c, s)) or existing_toggles.get(("", s)) or False
            tax_rows.append([
                "", s,
                sub_records.get((c, s), 0),
                sub_share.get((c, s), 0),
                sub_uplift.get((c, s), 0),
                sub_fix.get((c, s), ""),
                toggle,
            ])
    tax_ws.update(values=tax_rows, range_name="A1", value_input_option="USER_ENTERED")
    print(f"Created {tax_title}: {len(tax_rows) - 1} rows ({sum(1 for r in tax_rows[1:] if r[6] is True)} toggles preserved)")

    # ============================================================
    # 5. CREATE SEPARATOR TAB "Week of YYYY-MM-DD"
    # ============================================================
    sep_title = week_label
    ws_ids = {ws.title: ws.id for ws in sh.worksheets()}
    if sep_title not in ws_ids:
        sh.add_worksheet(title=sep_title, rows=20, cols=2)
        print(f"Created separator: {sep_title}")

    # ============================================================
    # 6. FORMATTING + REORDER
    # ============================================================
    ws_ids = {ws.title: ws.id for ws in sh.worksheets()}
    requests = []

    # Tracker new col formatting
    tsid = ws_ids["Tracker"]

    # Band header (row 1, new col): navy bold
    requests.append({
        "repeatCell": {
            "range": {"sheetId": tsid, "startRowIndex": 0, "endRowIndex": 1,
                      "startColumnIndex": next_col_idx, "endColumnIndex": next_col_idx + 1},
            "cell": {"userEnteredFormat": {
                "backgroundColor": NAVY,
                "textFormat": {"foregroundColor": WHITE, "bold": True},
                "horizontalAlignment": "CENTER",
                "verticalAlignment": "MIDDLE",
            }},
            "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)",
        }
    })
    # Band integer rows (2-3)
    requests.append({
        "repeatCell": {
            "range": {"sheetId": tsid, "startRowIndex": 1, "endRowIndex": 3,
                      "startColumnIndex": next_col_idx, "endColumnIndex": next_col_idx + 1},
            "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": "#,##0"}}},
            "fields": "userEnteredFormat.numberFormat",
        }
    })
    # Band rate rows (4-5)
    requests.append({
        "repeatCell": {
            "range": {"sheetId": tsid, "startRowIndex": 3, "endRowIndex": 5,
                      "startColumnIndex": next_col_idx, "endColumnIndex": next_col_idx + 1},
            "cell": {"userEnteredFormat": {"numberFormat": {"type": "PERCENT", "pattern": "0.00%"}}},
            "fields": "userEnteredFormat.numberFormat",
        }
    })

    # Top grid header
    requests.append({
        "repeatCell": {
            "range": {"sheetId": tsid, "startRowIndex": top_grid_header_idx,
                      "endRowIndex": top_grid_header_idx + 1,
                      "startColumnIndex": next_col_idx, "endColumnIndex": next_col_idx + 1},
            "cell": {"userEnteredFormat": {
                "backgroundColor": NAVY,
                "textFormat": {"foregroundColor": WHITE, "bold": True},
                "horizontalAlignment": "LEFT",
                "verticalAlignment": "MIDDLE",
                "wrapStrategy": "WRAP",
            }},
            "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment,wrapStrategy)",
        }
    })
    # Top grid data percent format
    if top_keys:
        requests.append({
            "repeatCell": {
                "range": {"sheetId": tsid, "startRowIndex": top_keys[0][2],
                          "endRowIndex": top_keys[-1][2] + 1,
                          "startColumnIndex": next_col_idx, "endColumnIndex": next_col_idx + 1},
                "cell": {"userEnteredFormat": {"numberFormat": {"type": "PERCENT", "pattern": "0.00%"}}},
                "fields": "userEnteredFormat.numberFormat",
            }
        })

    # Bottom grid header
    requests.append({
        "repeatCell": {
            "range": {"sheetId": tsid, "startRowIndex": bottom_grid_header_idx,
                      "endRowIndex": bottom_grid_header_idx + 1,
                      "startColumnIndex": next_col_idx, "endColumnIndex": next_col_idx + 1},
            "cell": {"userEnteredFormat": {
                "backgroundColor": NAVY,
                "textFormat": {"foregroundColor": WHITE, "bold": True},
                "horizontalAlignment": "LEFT",
                "verticalAlignment": "MIDDLE",
                "wrapStrategy": "WRAP",
            }},
            "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment,wrapStrategy)",
        }
    })

    # Tint cluster rows in both grids on the new col
    cluster_row_offsets = [(c, s, ri) for (c, s, ri) in top_keys if s == ""] + \
                          [(c, s, ri) for (c, s, ri) in bottom_keys if s == ""]
    for c, s, ri in cluster_row_offsets:
        requests.append({
            "repeatCell": {
                "range": {"sheetId": tsid, "startRowIndex": ri, "endRowIndex": ri + 1,
                          "startColumnIndex": next_col_idx, "endColumnIndex": next_col_idx + 1},
                "cell": {"userEnteredFormat": {
                    "backgroundColor": CLUSTER_TINT,
                    "textFormat": {"bold": True},
                }},
                "fields": "userEnteredFormat(backgroundColor,textFormat.bold)",
            }
        })

    # New col width
    requests.append({
        "updateDimensionProperties": {
            "range": {"sheetId": tsid, "dimension": "COLUMNS",
                      "startIndex": next_col_idx, "endIndex": next_col_idx + 1},
            "properties": {"pixelSize": 130},
            "fields": "pixelSize",
        }
    })

    # ----- Trailing-avg col formatting (mirrors the week col, top-grid only) -----
    tac = trailing_col_idx

    # Band label (row 0): navy bold, same as week header
    requests.append({
        "repeatCell": {
            "range": {"sheetId": tsid, "startRowIndex": 0, "endRowIndex": 1,
                      "startColumnIndex": tac, "endColumnIndex": tac + 1},
            "cell": {"userEnteredFormat": {
                "backgroundColor": NAVY,
                "textFormat": {"foregroundColor": WHITE, "bold": True},
                "horizontalAlignment": "CENTER",
                "verticalAlignment": "MIDDLE",
            }},
            "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)",
        }
    })
    # Band integer rows (1-2)
    requests.append({
        "repeatCell": {
            "range": {"sheetId": tsid, "startRowIndex": 1, "endRowIndex": 3,
                      "startColumnIndex": tac, "endColumnIndex": tac + 1},
            "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": "#,##0"}}},
            "fields": "userEnteredFormat.numberFormat",
        }
    })
    # Band rate rows (3-4)
    requests.append({
        "repeatCell": {
            "range": {"sheetId": tsid, "startRowIndex": 3, "endRowIndex": 5,
                      "startColumnIndex": tac, "endColumnIndex": tac + 1},
            "cell": {"userEnteredFormat": {"numberFormat": {"type": "PERCENT", "pattern": "0.00%"}}},
            "fields": "userEnteredFormat.numberFormat",
        }
    })
    # Top grid header
    requests.append({
        "repeatCell": {
            "range": {"sheetId": tsid, "startRowIndex": top_grid_header_idx,
                      "endRowIndex": top_grid_header_idx + 1,
                      "startColumnIndex": tac, "endColumnIndex": tac + 1},
            "cell": {"userEnteredFormat": {
                "backgroundColor": NAVY,
                "textFormat": {"foregroundColor": WHITE, "bold": True},
                "horizontalAlignment": "LEFT",
                "verticalAlignment": "MIDDLE",
                "wrapStrategy": "WRAP",
            }},
            "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment,wrapStrategy)",
        }
    })
    # Top grid data percent format
    if top_keys:
        requests.append({
            "repeatCell": {
                "range": {"sheetId": tsid, "startRowIndex": top_keys[0][2],
                          "endRowIndex": top_keys[-1][2] + 1,
                          "startColumnIndex": tac, "endColumnIndex": tac + 1},
                "cell": {"userEnteredFormat": {"numberFormat": {"type": "PERCENT", "pattern": "0.00%"}}},
                "fields": "userEnteredFormat.numberFormat",
            }
        })
    # Cluster row tinting on trailing col
    for c, s, ri in top_keys:
        if s != "":
            continue
        requests.append({
            "repeatCell": {
                "range": {"sheetId": tsid, "startRowIndex": ri, "endRowIndex": ri + 1,
                          "startColumnIndex": tac, "endColumnIndex": tac + 1},
                "cell": {"userEnteredFormat": {
                    "backgroundColor": CLUSTER_TINT,
                    "textFormat": {"bold": True},
                }},
                "fields": "userEnteredFormat(backgroundColor,textFormat.bold)",
            }
        })
    # Trailing col width
    requests.append({
        "updateDimensionProperties": {
            "range": {"sheetId": tsid, "dimension": "COLUMNS",
                      "startIndex": tac, "endIndex": tac + 1},
            "properties": {"pixelSize": 130},
            "fields": "pixelSize",
        }
    })

    # Toggle row styling: A6 right-aligned bold label, B6 centered numeric.
    requests.append({
        "repeatCell": {
            "range": {"sheetId": tsid, "startRowIndex": 5, "endRowIndex": 6,
                      "startColumnIndex": 0, "endColumnIndex": 1},
            "cell": {"userEnteredFormat": {
                "textFormat": {"bold": True},
                "horizontalAlignment": "RIGHT",
                "verticalAlignment": "MIDDLE",
            }},
            "fields": "userEnteredFormat(textFormat.bold,horizontalAlignment,verticalAlignment)",
        }
    })
    requests.append({
        "repeatCell": {
            "range": {"sheetId": tsid, "startRowIndex": 5, "endRowIndex": 6,
                      "startColumnIndex": 1, "endColumnIndex": 2},
            "cell": {"userEnteredFormat": {
                "numberFormat": {"type": "NUMBER", "pattern": "0"},
                "horizontalAlignment": "CENTER",
                "verticalAlignment": "MIDDLE",
                "backgroundColor": {"red": 1.0, "green": 0.95, "blue": 0.80},
                "textFormat": {"bold": True},
            }},
            "fields": "userEnteredFormat(numberFormat,horizontalAlignment,verticalAlignment,backgroundColor,textFormat.bold)",
        }
    })

    # Findings tab formatting
    f_sid = ws_ids[findings_title]
    requests.append({
        "repeatCell": {
            "range": {"sheetId": f_sid, "startRowIndex": 0, "endRowIndex": 1,
                      "startColumnIndex": 0, "endColumnIndex": len(FINDINGS_HEADERS)},
            "cell": {"userEnteredFormat": {
                "backgroundColor": NAVY,
                "textFormat": {"foregroundColor": WHITE, "bold": True},
                "horizontalAlignment": "LEFT",
                "verticalAlignment": "MIDDLE",
                "wrapStrategy": "WRAP",
            }},
            "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment,wrapStrategy)",
        }
    })
    requests.append({
        "updateSheetProperties": {
            "properties": {"sheetId": f_sid, "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount",
        }
    })

    # Taxonomy output formatting
    taxsid = ws_ids[tax_title]
    requests.append({
        "repeatCell": {
            "range": {"sheetId": taxsid, "startRowIndex": 0, "endRowIndex": 1,
                      "startColumnIndex": 0, "endColumnIndex": 7},
            "cell": {"userEnteredFormat": {
                "backgroundColor": NAVY,
                "textFormat": {"foregroundColor": WHITE, "bold": True},
                "horizontalAlignment": "LEFT",
                "verticalAlignment": "MIDDLE",
                "wrapStrategy": "WRAP",
            }},
            "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment,wrapStrategy)",
        }
    })
    # Checkbox validation on subcluster rows of col G (Build review tab)
    for ri in tax_subcluster_row_idxs:
        requests.append({
            "setDataValidation": {
                "range": {"sheetId": taxsid, "startRowIndex": ri, "endRowIndex": ri + 1,
                          "startColumnIndex": 6, "endColumnIndex": 7},
                "rule": {"condition": {"type": "BOOLEAN"}, "strict": True, "showCustomUi": True}
            }
        })
    requests.append({
        "repeatCell": {
            "range": {"sheetId": taxsid, "startRowIndex": 1, "endRowIndex": len(tax_rows),
                      "startColumnIndex": 2, "endColumnIndex": 3},
            "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": "#,##0"}}},
            "fields": "userEnteredFormat.numberFormat",
        }
    })
    requests.append({
        "repeatCell": {
            "range": {"sheetId": taxsid, "startRowIndex": 1, "endRowIndex": len(tax_rows),
                      "startColumnIndex": 3, "endColumnIndex": 4},
            "cell": {"userEnteredFormat": {"numberFormat": {"type": "PERCENT", "pattern": "0.0%"}}},
            "fields": "userEnteredFormat.numberFormat",
        }
    })
    requests.append({
        "repeatCell": {
            "range": {"sheetId": taxsid, "startRowIndex": 1, "endRowIndex": len(tax_rows),
                      "startColumnIndex": 4, "endColumnIndex": 5},
            "cell": {"userEnteredFormat": {"numberFormat": {"type": "PERCENT", "pattern": "0.00%"}}},
            "fields": "userEnteredFormat.numberFormat",
        }
    })
    for c0, c1 in [(1, 2), (5, 6)]:
        requests.append({
            "repeatCell": {
                "range": {"sheetId": taxsid, "startRowIndex": 1, "endRowIndex": len(tax_rows),
                          "startColumnIndex": c0, "endColumnIndex": c1},
                "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP", "verticalAlignment": "TOP"}},
                "fields": "userEnteredFormat.wrapStrategy,userEnteredFormat.verticalAlignment",
            }
        })
    for ri in tax_cluster_row_idxs:
        requests.append({
            "repeatCell": {
                "range": {"sheetId": taxsid, "startRowIndex": ri, "endRowIndex": ri + 1,
                          "startColumnIndex": 0, "endColumnIndex": 6},
                "cell": {"userEnteredFormat": {
                    "backgroundColor": CLUSTER_TINT,
                    "textFormat": {"bold": True},
                }},
                "fields": "userEnteredFormat(backgroundColor,textFormat.bold)",
            }
        })
    requests.append({
        "updateSheetProperties": {
            "properties": {"sheetId": taxsid,
                           "gridProperties": {"frozenRowCount": 1, "frozenColumnCount": 2}},
            "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount",
        }
    })
    for c0, c1, w in [(0, 1, 220), (1, 2, 320), (2, 3, 130), (3, 4, 110), (4, 5, 140), (5, 6, 480), (6, 7, 140)]:
        requests.append({
            "updateDimensionProperties": {
                "range": {"sheetId": taxsid, "dimension": "COLUMNS",
                          "startIndex": c0, "endIndex": c1},
                "properties": {"pixelSize": w},
                "fields": "pixelSize",
            }
        })

    # Separator tab color
    if sep_title in ws_ids:
        requests.append({
            "updateSheetProperties": {
                "properties": {"sheetId": ws_ids[sep_title], "tabColor": SEPARATOR_TINT},
                "fields": "tabColor",
            }
        })

    # Reorder tabs: Tracker, Opportunities, Review queue, Taxonomy, then per-week groups newest-first
    desired_order = ["Tracker", "Opportunities", "Review queue", "Taxonomy"]
    week_dates_seen = sorted(
        {t.split(" ")[-1] for t in ws_ids.keys() if re.match(r"^Findings \d{4}-\d{2}-\d{2}$", t)},
        reverse=True,
    )
    for d in week_dates_seen:
        for prefix in [f"Week of {d}", f"Findings {d}", f"Taxonomy output {d}", f"Review {d}"]:
            if prefix in ws_ids:
                desired_order.append(prefix)
    for idx, title in enumerate(desired_order):
        if title in ws_ids:
            requests.append({
                "updateSheetProperties": {
                    "properties": {"sheetId": ws_ids[title], "index": idx},
                    "fields": "index",
                }
            })

    sheets_api.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body={"requests": requests}).execute()
    print(f"Applied {len(requests)} formatting/reorder requests")

    print(f"\nDone. Sheet: https://docs.google.com/spreadsheets/d/{sheet_id}/edit")


if __name__ == "__main__":
    main()
