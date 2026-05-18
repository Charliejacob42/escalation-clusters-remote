#!/usr/bin/env python3
"""Build adapter inputs for the bot-data and Front-data pulls.

Usage: build_inputs.py <sample.json> <propelix_input.json> <front_input.json>
"""
import json
import sys


def main():
    if len(sys.argv) < 4:
        print("Usage: build_inputs.py <sample.json> <propelix_input.json> <front_input.json>", file=sys.stderr)
        sys.exit(1)
    sample_path, propelix_out, front_out = sys.argv[1], sys.argv[2], sys.argv[3]

    with open(sample_path) as f:
        sample = json.load(f)

    propelix_input = []
    front_input = []
    for r in sample:
        idx = r.get("index")
        propelix_input.append({
            "index": idx,
            "conversation_id": r.get("conversation_id", ""),
            "escalation_timestamp": r.get("escalation_timestamp", ""),
        })
        front_input.append({
            "index": idx,
            "front_conversation_id": r.get("front_conversation_id", ""),
            "escalation_timestamp": r.get("escalation_timestamp", ""),
        })

    with open(propelix_out, "w") as f:
        json.dump(propelix_input, f, indent=2)
    with open(front_out, "w") as f:
        json.dump(front_input, f, indent=2)
    print(f"Wrote {len(sample)} records to {propelix_out} and {front_out}")


if __name__ == "__main__":
    main()
