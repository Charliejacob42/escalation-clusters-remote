#!/usr/bin/env python3
"""Merge sample + bot data + Front data into enriched analyst-ready records.

Usage: build_enriched.py <sample.json> <propelix_output.json> <front_output.json> <enriched.json>
"""
import json
import sys

STAGE_NAME = {
    0: "pre-onboarding", 1: "signed up, quoting incomplete", 3: "quoting in progress",
    4: "quotes available", 8: "edited, awaiting reprocessing", 9: "confirmed rate requested",
    10: "confirmed rate provided", 11: "switch request submitted", 12: "current/prior policyholder"
}


def main():
    if len(sys.argv) < 5:
        print("Usage: build_enriched.py <sample.json> <propelix.json> <front.json> <out.json>", file=sys.stderr)
        sys.exit(1)
    s_path, p_path, f_path, out_path = sys.argv[1:5]
    with open(s_path) as f: sample = json.load(f)
    with open(p_path) as f: propelix = json.load(f)
    with open(f_path) as f: front = json.load(f)

    p_by = {p["index"]: p for p in propelix}
    f_by = {x["index"]: x for x in front}

    enriched = []
    for r in sample:
        i = r["index"]
        p = p_by.get(i, {})
        x = f_by.get(i, {})

        bot_thread = "\n".join(p.get("thread", []))[:5000]

        # Front agent thread; fall back to inline_agent_messages
        f_msgs = [m for m in x.get("messages", []) if m.get("author_type") in ("agent", "teammate")]
        f_msgs.sort(key=lambda m: m.get("created_at", 0))
        agent_parts = [f"[{m.get('author','?')}] {m.get('body_text','')[:400]}"
                       for m in f_msgs[:10] if m.get("body_text")]
        agent_thread = "\n".join(agent_parts)[:2000]
        if not agent_thread.strip():
            inline = p.get("inline_agent_messages", [])
            agent_parts = [f"[{m.get('author','?')}] {m.get('text','')[:400]}" for m in inline[:10]]
            agent_thread = "\n".join(agent_parts)[:2000]
            agent_thread_source = "inline_bot_thread" if agent_thread else "none"
        else:
            agent_thread_source = "front"

        comments = x.get("comments", [])
        comment_summary = " | ".join(c.get("body", "")[:150] for c in comments[:3])[:500]

        stage = r.get("user_stage")
        stage_int = int(stage) if stage is not None else None
        is_ph = r.get("is_policyholder")

        enriched.append({
            "index": i,
            "user_id": int(float(r.get("dec_user_id") or 0)),
            "display_name": r.get("display_name"),
            "escalation_type": r.get("escalation_type"),
            "escalation_path": r.get("escalation_path"),
            "team": r.get("team"),
            "channel": r.get("channel"),
            "escalation_timestamp": r.get("escalation_timestamp"),
            "propelix_link": r.get("propelix_link"),
            "front_link": r.get("front_link"),
            "crm_link": r.get("crm_link"),
            "user_stage_id": stage_int,
            "user_stage_label": STAGE_NAME.get(stage_int, "unknown") if stage_int is not None else None,
            "is_policyholder": bool(is_ph) if is_ph is not None else None,
            "tools_called": p.get("tools_called", []),
            "bot_thread": bot_thread,
            "bot_message_count": p.get("message_count", 0),
            "agent_thread": agent_thread,
            "agent_thread_source": agent_thread_source,
            "front_signals": x.get("signals", {}),
            "front_metadata": x.get("metadata", {}),
            "comment_summary": comment_summary,
        })

    with open(out_path, "w") as f:
        json.dump(enriched, f, indent=2, default=str)
    print(f"Built {len(enriched)} enriched records.")
    print(f"  with bot thread: {sum(1 for r in enriched if r['bot_thread'])}")
    print(f"  with agent thread: {sum(1 for r in enriched if r['agent_thread'])}")
    print(f"  with user_stage: {sum(1 for r in enriched if r['user_stage_id'] is not None)}")


if __name__ == "__main__":
    main()
