#!/usr/bin/env python3
"""Split enriched records into N batches for parallel analyst agents.

Usage: split_batches.py <enriched.json> <output_prefix> <num_batches>
Writes <output_prefix>_1.json ... <output_prefix>_N.json
"""
import json
import sys


def main():
    if len(sys.argv) < 4:
        print("Usage: split_batches.py <enriched.json> <output_prefix> <num_batches>", file=sys.stderr)
        sys.exit(1)
    in_path, prefix, num = sys.argv[1], sys.argv[2], int(sys.argv[3])
    with open(in_path) as f:
        records = json.load(f)
    size = (len(records) + num - 1) // num
    for i in range(num):
        batch = records[i*size:(i+1)*size]
        with open(f"{prefix}_{i+1}.json", "w") as f:
            json.dump(batch, f, indent=2, default=str)
        print(f"  batch {i+1}: {len(batch)} records -> {prefix}_{i+1}.json")


if __name__ == "__main__":
    main()
