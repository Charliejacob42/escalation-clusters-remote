#!/usr/bin/env python3
"""
Pull Front API data for a batch of escalation conversations.
Reusable script called by gathering agents. Reads a batch JSON, pulls
metadata/messages/comments per conversation, extracts signals, saves enriched output.

Usage:
    python3 pull_front_batch.py <batch_input.json> <output.json>

Input JSON: array of objects, each with at minimum:
    - front_conversation_id (string, may be empty)
    - escalation_timestamp (string, ISO-ish datetime)
    - index (int)
"""

import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TOKEN_PATH = "/Users/charliejacob/.claude/front-api-token.txt"
BASE_URL = "https://api2.frontapp.com"

# ── HTML stripping ────────────────────────────────────────────────────────────

class HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.reset()
        self.fed = []

    def handle_data(self, d):
        self.fed.append(d)

    def get_data(self):
        return " ".join(self.fed)


def strip_html(html_str):
    if not html_str:
        return ""
    html_str = re.sub(r"<br\s*/?>", " ", html_str, flags=re.IGNORECASE)
    html_str = re.sub(r"</p>", " ", html_str, flags=re.IGNORECASE)
    html_str = re.sub(r"</div>", " ", html_str, flags=re.IGNORECASE)
    html_str = re.sub(r"</li>", " ", html_str, flags=re.IGNORECASE)
    s = HTMLStripper()
    try:
        s.feed(html_str)
        text = s.get_data()
    except Exception:
        text = re.sub(r"<[^>]+>", " ", html_str)
    return re.sub(r"\s+", " ", text).strip()


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def api_get(path, token, session):
    url = f"{BASE_URL}{path}"
    try:
        resp = session.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
            verify=False,
        )
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response else "?"
        body = e.response.text[:200] if e.response else ""
        print(f"  HTTP {code} for {url}: {body}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  Error for {url}: {e}", file=sys.stderr)
        return None


def get_all_pages(path, token, session, key="_results", window_start=None):
    """Collect all pages. Early-terminate when all items on a page predate window_start."""
    results = []
    data = api_get(path, token, session)
    if data is None:
        return results
    page_items = data.get(key, [])
    results.extend(page_items)

    while True:
        pagination = data.get("_pagination", {})
        next_url = pagination.get("next")
        if not next_url:
            break
        # Early termination: if all items on this page are before window start
        if window_start and page_items:
            timestamps = [i.get("created_at", 0) for i in page_items if isinstance(i.get("created_at"), (int, float))]
            if timestamps and max(timestamps) < window_start:
                break
        try:
            resp = session.get(
                next_url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
                verify=False,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  Pagination error: {e}", file=sys.stderr)
            break
        page_items = data.get(key, [])
        results.extend(page_items)
    return results


# ── Signal extraction ─────────────────────────────────────────────────────────

CALLOUT_RESULT_PATTERNS = [
    r"callout\s+has\s+been\s+completed",
    r"callout\s+completed",
    r"callout\s+relayed",
    r"relayed\s+(?:the\s+)?response\s+from\s+(?:the\s+)?(?:co|carrier)",
    r"carrier\s+callout\s+(?:completed|done|relayed)",
]
PORTAL_PATTERNS = [
    r"logged?\s+(?:in|into)\s+(?:the\s+)?(?:carrier\s+)?portal",
    r"carrier\s+portal", r"portal\s+login", r"logging\s+into", r"log\s+in\s+to",
]
CRM_PATTERNS = [
    r"push\s+sale", r"pushed\s+(?:the\s+)?sale",
    r"edit\s+(?:the\s+)?profile", r"edited\s+(?:the\s+)?profile",
    r"updated?\s+(?:the\s+)?(?:crm|profile|record)",
    r"review(?:ed|ing)?\s+(?:the\s+)?docs?", r"review(?:ed|ing)?\s+documents?",
    r"crm\s+action", r"in\s+(?:the\s+)?crm",
]
NOT_POSSIBLE_PATTERNS = [
    r"(?:not|can'?t|cannot|unable|don'?t)\s+(?:be\s+able\s+to\s+)?help",
    r"not\s+possible", r"isn'?t\s+possible",
    r"not\s+something\s+(?:i|we)\s+(?:can|am\s+able)",
    r"outside\s+(?:of\s+)?(?:my|our)\s+(?:scope|ability|control)",
    r"i'?m\s+afraid\s+(?:i|we)\s+(?:can'?t|cannot)",
    r"unfortunately\s+(?:i|we)\s+(?:can'?t|cannot)",
]


def search_patterns(text, patterns):
    text_lower = text.lower()
    for pat in patterns:
        m = re.search(pat, text_lower)
        if m:
            start = max(0, m.start() - 40)
            end = min(len(text), m.end() + 80)
            return True, text[start:end].strip()
    return False, ""


UI_ARTIFACT_RE = re.compile(r"callout\s+\w\s+\w\b", re.IGNORECASE)
UI_ENTITY_RE = re.compile(r"&[a-z]+;", re.IGNORECASE)


def is_ui_artifact_match(snippet):
    """Reject snippets that look like Front UI fragments rather than real callout language."""
    if not snippet:
        return False
    if "Posted a callout" in snippet:
        return True
    if UI_ARTIFACT_RE.search(snippet):
        return True
    if UI_ENTITY_RE.search(snippet):
        return True
    return False


def detect_callout(tags, comment_text, agent_text):
    """Two-layer callout detection per corrections.md [2026-04-03].

    Layer 1: gold-standard Front tag (RTC Callout*, Callout).
    Layer 2: result-indicator regex match in comments/agent text, rejecting UI artifacts.
    """
    callout_tags = [
        t for t in (tags or [])
        if t and ("rtc callout" in t.lower() or t.strip().lower() == "callout")
    ]
    if callout_tags:
        return True, f"tag: {callout_tags[0]}"
    for source in (comment_text, agent_text):
        has, ev = search_patterns(source, CALLOUT_RESULT_PATTERNS)
        if has and not is_ui_artifact_match(ev):
            return True, ev
    return False, ""


def extract_signals(messages, comments, tags=None):
    comment_text = " ".join(c.get("body", "") for c in comments)
    agent_msgs = [m for m in messages if m.get("author_type") in ("agent", "teammate")]
    agent_text = " ".join(m.get("body_text", "") for m in agent_msgs)

    has_callout, callout_ev = detect_callout(tags, comment_text, agent_text)
    has_portal, portal_ev = search_patterns(agent_text, PORTAL_PATTERNS)
    if not has_portal:
        has_portal, portal_ev = search_patterns(comment_text, PORTAL_PATTERNS)
    has_crm, crm_ev = search_patterns(agent_text + " " + comment_text, CRM_PATTERNS)
    has_not_possible, not_possible_ev = search_patterns(agent_text, NOT_POSSIBLE_PATTERNS)

    summary_parts = []
    for m in agent_msgs[:6]:
        snippet = m.get("body_text", "")[:200]
        if snippet:
            summary_parts.append(snippet)

    return {
        "has_carrier_callout": has_callout,
        "callout_evidence": callout_ev,
        "has_carrier_portal_login": has_portal,
        "portal_evidence": portal_ev,
        "has_crm_action": has_crm,
        "crm_evidence": crm_ev,
        "agent_could_not_help": has_not_possible,
        "not_possible_evidence": not_possible_ev,
        "agent_action_summary": " | ".join(summary_parts[:3])[:500] if summary_parts else "No agent messages found.",
    }


# ── Normalizers ───────────────────────────────────────────────────────────────

def normalize_message(msg):
    author = msg.get("author") or {}
    if author.get("is_bot"):
        author_type = "bot"
    elif author.get("email", "").endswith("@jerry.ai") or author.get("username", "").endswith("jerry.ai"):
        author_type = "agent"
    elif "username" in author and author.get("username"):
        author_type = "teammate"
    else:
        author_type = msg.get("type", "unknown")
    author_name = author.get("name") or author.get("email") or author.get("username") or "Unknown"
    body_html = msg.get("body") or msg.get("text") or ""
    body_text = strip_html(body_html)
    return {
        "id": msg.get("id", ""),
        "type": msg.get("type", ""),
        "author_type": author_type,
        "author": author_name,
        "body_text": body_text[:2000],
        "created_at": msg.get("created_at", ""),
    }


def normalize_comment(c):
    author = c.get("author") or {}
    author_name = author.get("name") or author.get("email") or "Unknown"
    return {
        "id": c.get("id", ""),
        "author": author_name,
        "body": (c.get("body") or "")[:1000],
        "created_at": c.get("created_at", ""),
    }


def extract_metadata(conv):
    tags = [t.get("name", "") for t in (conv.get("tags") or [])]
    assignee_obj = conv.get("assignee") or {}
    assignee = assignee_obj.get("name") or assignee_obj.get("email") or "Unassigned"
    status = conv.get("status", "")
    return {"tags": tags, "assignee": assignee, "status": status}


# ── Time window helpers ───────────────────────────────────────────────────────

def parse_timestamp(ts_str):
    """Parse escalation_timestamp (ISO 8601 with offset, naive PT, or naive UTC) to unix timestamp."""
    if not ts_str:
        return None
    try:
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone(timedelta(hours=-7)))
        return dt.timestamp()
    except (ValueError, TypeError):
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            dt = datetime.strptime(ts_str, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone(timedelta(hours=-7)))
            return dt.timestamp()
        except ValueError:
            continue
    return None


def filter_by_window(items, esc_ts, days_before=7, days_after=3):
    """Filter items to [esc_ts - days_before, esc_ts + days_after]."""
    if esc_ts is None:
        return items
    window_start = esc_ts - (days_before * 86400)
    window_end = esc_ts + (days_after * 86400)
    return [
        i for i in items
        if isinstance(i.get("created_at"), (int, float))
        and window_start <= i["created_at"] <= window_end
    ]


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 3:
        print("Usage: pull_front_batch.py <input.json> <output.json>", file=sys.stderr)
        sys.exit(1)

    input_path = sys.argv[1]
    output_path = sys.argv[2]

    with open(TOKEN_PATH) as f:
        token = f.read().strip()

    with open(input_path) as f:
        batch = json.load(f)

    session = requests.Session()
    results = []

    for i, rec in enumerate(batch):
        cnv_id = rec.get("front_conversation_id", "")
        idx = rec.get("index", i + 1)

        if not cnv_id:
            results.append({
                "index": idx,
                "front_conversation_id": "",
                "metadata": {"tags": [], "assignee": "N/A", "status": "N/A"},
                "messages": [],
                "comments": [],
                "signals": {
                    "has_carrier_callout": False, "callout_evidence": "",
                    "has_carrier_portal_login": False, "portal_evidence": "",
                    "has_crm_action": False, "crm_evidence": "",
                    "agent_could_not_help": False, "not_possible_evidence": "",
                    "agent_action_summary": "No Front data.",
                },
                "error": None,
            })
            print(f"  [{idx}] Skipped (no Front ID)", flush=True)
            continue

        print(f"  [{idx}] {cnv_id} ...", end="", flush=True)

        # Parse escalation timestamp for time window
        esc_ts = parse_timestamp(rec.get("escalation_timestamp", ""))
        window_start_unix = (esc_ts - 7 * 86400) if esc_ts else None

        # 1. Metadata
        conv_data = api_get(f"/conversations/{cnv_id}", token, session)
        time.sleep(0.3)

        # 2. Messages (with early termination)
        msgs_raw = get_all_pages(
            f"/conversations/{cnv_id}/messages", token, session,
            window_start=window_start_unix
        )
        time.sleep(0.3)

        # 3. Comments
        comments_raw = get_all_pages(
            f"/conversations/{cnv_id}/comments", token, session,
            window_start=window_start_unix
        )
        time.sleep(0.3)

        # Check for 401 on first conversation
        if i == 0 and conv_data is None:
            print("\nERROR: First conversation returned no data. Token may be expired.", file=sys.stderr)
            print("Refresh at ~/.claude/front-api-token.txt", file=sys.stderr)
            sys.exit(1)

        # Normalize
        metadata = extract_metadata(conv_data) if conv_data else {"tags": [], "assignee": "ERROR", "status": "ERROR"}

        # Filter by time window
        if esc_ts:
            msgs_raw = filter_by_window(msgs_raw, esc_ts)
            comments_raw = filter_by_window(comments_raw, esc_ts)

        messages = [normalize_message(m) for m in msgs_raw]
        comments = [normalize_comment(c) for c in comments_raw]
        signals = extract_signals(messages, comments, metadata.get("tags", []))

        results.append({
            "index": idx,
            "front_conversation_id": cnv_id,
            "metadata": metadata,
            "messages": messages,
            "comments": comments,
            "signals": signals,
            "error": None,
        })

        print(f" msgs={len(messages)} comments={len(comments)}"
              f" callout={signals['has_carrier_callout']}", flush=True)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    print(f"\nDone. {len(results)} conversations -> {output_path}")


if __name__ == "__main__":
    main()
