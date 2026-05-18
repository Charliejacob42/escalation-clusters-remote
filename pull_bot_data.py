#!/usr/bin/env python3
"""Pull Propelix bot conversation data for escalation_clusters skill.

Improvements over pull_propelix_batch.py:
- Returns chronological-order thread (oldest -> newest)
- Strips raw JSON tool-result blocks, replaces with brief tool markers
- Larger pagination limit (100) to reduce risk of missing tool calls
- Extracts inline agent messages as a fallback signal
"""
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

# Matches a JSON-tool-result blob whose first key is "result", tolerating any
# whitespace (spaces, tabs, newlines from pretty-printing) between { and "result".
JSON_RESULT_PREFIX_RE = re.compile(r'^\s*\{\s*"result"\s*:')


def parse_escalation_ts(ts_str):
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str)
    except (ValueError, TypeError):
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(ts_str, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone(timedelta(hours=-7)))
            return dt
        except ValueError:
            continue
    return None


def parse_propelix_ts(ts_str):
    if not ts_str:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%f+00:00"):
        try:
            return datetime.strptime(ts_str, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        from dateutil import parser as dp
        return dp.isoparse(ts_str)
    except Exception:
        return None


def filter_by_window(messages, esc_dt, days_before=7, days_after=3):
    if not esc_dt or not messages:
        return messages
    window_start = esc_dt - timedelta(days=days_before)
    window_end = esc_dt + timedelta(days=days_after)
    out = []
    for m in messages:
        if not isinstance(m, dict):
            out.append(m); continue
        msg_dt = parse_propelix_ts(m.get("createdAt", ""))
        if msg_dt is None or (window_start <= msg_dt <= window_end):
            out.append(m)
    return out


def is_json_tool_result(text):
    if not text:
        return False
    # Check the first ~256 chars to allow for pretty-printed JSON where a few
    # bytes of whitespace can sit between { and "result". 64 was too tight.
    return bool(JSON_RESULT_PREFIX_RE.match(text[:256]))


def extract_tool_name_from_result(text):
    """Best-effort: find tool name in a JSON-ish tool result blob. Returns 'unknown' if not found."""
    m = re.search(r'"tool"\s*:\s*"([^"]+)"', text or "")
    if m:
        return m.group(1)
    m = re.search(r'"name"\s*:\s*"([^"]+)"', text or "")
    if m:
        return m.group(1)
    return "unknown"


def extract_thread(messages):
    """Build chronological labeled thread. Strips JSON tool-result blocks."""
    # Sort ascending by createdAt
    def key(m):
        if isinstance(m, dict):
            return parse_propelix_ts(m.get("createdAt", "")) or datetime.min.replace(tzinfo=timezone.utc)
        return datetime.min.replace(tzinfo=timezone.utc)

    msgs_sorted = sorted([m for m in messages if isinstance(m, dict)], key=key)
    thread = []
    for m in msgs_sorted:
        from_info = m.get("from") or {}
        if not isinstance(from_info, dict):
            from_info = {}
        msg_type = from_info.get("type", "unknown")
        msg_name = from_info.get("name", "")
        text = m.get("text", "") or ""
        if not text.strip():
            continue

        # Strip JSON tool result blobs
        if msg_type == "bot" and is_json_tool_result(text):
            tool_name = extract_tool_name_from_result(text)
            thread.append(f"[BOT-TOOL] <{tool_name} result>")
            continue

        if msg_type == "self":
            label = f"[USER: {msg_name}]" if msg_name else "[USER]"
        elif msg_type == "bot":
            label = "[BOT]"
        elif msg_type == "agent":
            label = f"[AGENT: {msg_name}]" if msg_name else "[AGENT]"
        else:
            label = f"[{msg_type.upper()}]"

        tools = m.get("calledTools") or []
        tool_str = ""
        if tools:
            names = []
            for t in tools:
                names.append(t.get("name", str(t)) if isinstance(t, dict) else str(t))
            tool_str = f" [Tools: {', '.join(names)}]"
        thread.append(f"{label}{tool_str} {text.strip()[:1000]}")
    return thread


def get_conversation_messages(conv_id, page_size=100):
    all_msgs = []
    offset = 0
    total = None
    while True:
        try:
            proc = subprocess.run(
                ["npx", "propelix-cli@latest", "conversation", "get", conv_id,
                 "--limit", str(page_size), "--offset", str(offset)],
                capture_output=True, text=True, timeout=60
            )
            if proc.returncode != 0:
                return None, None, f"exit {proc.returncode}: {proc.stderr[:200]}"
            stdout = proc.stdout
            data = None
            for i, ch in enumerate(stdout):
                if ch in ("{", "["):
                    try:
                        data = json.loads(stdout[i:])
                        break
                    except json.JSONDecodeError:
                        continue
            if data is None:
                return None, None, f"no JSON: {stdout[:200]}"
            if not isinstance(data, dict):
                return data, None, None
            msgs_obj = data.get("messages", {})
            if isinstance(msgs_obj, dict):
                items = msgs_obj.get("items", [])
                if total is None:
                    total = msgs_obj.get("total", len(items))
            elif isinstance(msgs_obj, list):
                items = msgs_obj
                if total is None:
                    total = len(items)
            else:
                items = []
            all_msgs.extend(items)
            if len(items) < page_size or len(all_msgs) >= (total or 0):
                break
            offset += page_size
            time.sleep(0.3)
        except subprocess.TimeoutExpired:
            return None, None, "timeout"
        except Exception as e:
            return None, None, str(e)
    return data, all_msgs, None


def main():
    if len(sys.argv) < 3:
        print("Usage: pull_bot_data.py <input.json> <output.json>", file=sys.stderr)
        sys.exit(1)
    in_path, out_path = sys.argv[1], sys.argv[2]
    with open(in_path) as f:
        records = json.load(f)

    out = []
    errors = 0
    for i, rec in enumerate(records, start=1):
        conv = rec.get("conversation_id") or rec.get("propelix_conversation_id") or ""
        if not conv:
            out.append({
                "index": rec.get("index", i),
                "conversation_id": "",
                "thread": [],
                "tools_called": [],
                "inline_agent_messages": [],
                "message_count": 0,
                "error": "no conversation_id",
            })
            print(f"  [{i}/{len(records)}] skip (no conv)", flush=True)
            continue

        print(f"  [{i}/{len(records)}] {conv[:15]}...", end="", flush=True)
        data, msgs, err = get_conversation_messages(conv)
        if err:
            out.append({
                "index": rec.get("index", i),
                "conversation_id": conv,
                "thread": [],
                "tools_called": [],
                "inline_agent_messages": [],
                "message_count": 0,
                "error": err,
            })
            errors += 1
            print(f" ERR: {err[:60]}", flush=True)
            continue

        # Apply time-window filter
        esc_dt = parse_escalation_ts(rec.get("escalation_timestamp") or rec.get("escalation_time", ""))
        original = len(msgs or [])
        msgs = filter_by_window(msgs or [], esc_dt)

        thread = extract_thread(msgs)

        # Tools called (across all messages, deduped)
        tools = []
        for m in msgs:
            if isinstance(m, dict):
                for t in (m.get("calledTools") or []):
                    name = t.get("name", str(t)) if isinstance(t, dict) else str(t)
                    tools.append(name)
        tools_unique = list(dict.fromkeys(tools))

        # Inline agent messages (from.type == 'agent')
        inline_agents = []
        for m in msgs:
            if isinstance(m, dict) and (m.get("from") or {}).get("type") == "agent":
                name = (m.get("from") or {}).get("name", "")
                txt = (m.get("text") or "")[:600]
                if txt.strip():
                    inline_agents.append({"author": name, "text": txt, "created_at": m.get("createdAt")})

        out.append({
            "index": rec.get("index", i),
            "conversation_id": conv,
            "thread": thread,
            "tools_called": tools_unique,
            "inline_agent_messages": inline_agents,
            "message_count": len(msgs),
            "original_message_count": original,
            "error": None,
        })
        print(f" msgs={len(msgs)}/{original} tools={len(tools_unique)} inline_agents={len(inline_agents)}", flush=True)
        time.sleep(0.4)

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"\nDone. {len(out)} convs, {errors} errors -> {out_path}")


if __name__ == "__main__":
    main()
