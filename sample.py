#!/usr/bin/env python3
"""Proportional stratified sample from a population.json. Stratify by escalation_type.

Usage: sample.py <population.json> <output.json> <target_n> [seed]
"""
import json
import random
import sys
from collections import Counter, defaultdict


def main():
    if len(sys.argv) < 4:
        print("Usage: sample.py <population.json> <output.json> <target_n> [seed]", file=sys.stderr)
        sys.exit(1)
    pop_path, out_path, target_n = sys.argv[1], sys.argv[2], int(sys.argv[3])
    seed = int(sys.argv[4]) if len(sys.argv) > 4 else 42
    random.seed(seed)

    with open(pop_path) as f:
        rows = json.load(f)
    total_in = len(rows)
    rows = [r for r in rows if r.get("escalation_type")]
    dropped = total_in - len(rows)
    if dropped:
        print(f"WARNING: dropped {dropped} rows with missing escalation_type", file=sys.stderr)
    type_counts = Counter(r["escalation_type"] for r in rows)
    total = len(rows)

    if target_n > total:
        raise ValueError(
            f"Cannot draw {target_n} samples from a population of only {total} rows"
        )

    allocations = {}
    remainders = []
    for t, c in type_counts.items():
        raw = target_n * c / total
        allocations[t] = int(raw)
        remainders.append((t, raw - int(raw)))
    allocated = sum(allocations.values())
    remainders.sort(key=lambda x: (-x[1], x[0]))
    for t, _ in remainders[: target_n - allocated]:
        allocations[t] += 1

    # Quota redistribution: if any stratum is allocated more rows than it has,
    # cap it at its population and redistribute the deficit to non-shorted strata
    # via Hamilton's largest-remainder method, weighted by remaining capacity.
    shorted = sorted(
        t for t, n in allocations.items() if n > type_counts[t]
    )
    if shorted:
        deficit = 0
        for t in shorted:
            short_amount = allocations[t] - type_counts[t]
            print(
                f"WARNING: stratum '{t}' had only {type_counts[t]} rows but was "
                f"allocated {allocations[t]}; {short_amount} quota redistributed",
                file=sys.stderr,
            )
            deficit += short_amount
            allocations[t] = type_counts[t]

        recipients = sorted(
            t for t in allocations
            if t not in set(shorted) and type_counts[t] - allocations[t] > 0
        )
        while deficit > 0:
            capacities = {t: type_counts[t] - allocations[t] for t in recipients}
            cap_total = sum(capacities.values())
            if cap_total == 0:
                raise ValueError(
                    f"Cannot redistribute {deficit} quota; no stratum has remaining "
                    f"capacity. This should not happen when target_n <= len(rows)."
                )
            # Largest-remainder allocation of `deficit` across recipients,
            # weighted by remaining capacity.
            take = min(deficit, cap_total)
            extra = {}
            extra_remainders = []
            for t in recipients:
                raw = take * capacities[t] / cap_total
                extra[t] = int(raw)
                extra_remainders.append((t, raw - int(raw)))
            assigned = sum(extra.values())
            extra_remainders.sort(key=lambda x: (-x[1], x[0]))
            for t, _ in extra_remainders[: take - assigned]:
                extra[t] += 1
            # Apply, capping at capacity (largest-remainder math is float-stable
            # but cap defensively).
            for t in recipients:
                grant = min(extra[t], capacities[t])
                allocations[t] += grant
                deficit -= grant
            recipients = sorted(
                t for t in recipients if type_counts[t] - allocations[t] > 0
            )

    by_type = defaultdict(list)
    for r in rows:
        by_type[r["escalation_type"]].append(r)
    sampled = []
    for t, n in allocations.items():
        if n > 0:
            if n > len(by_type[t]):
                raise ValueError(
                    f"Stratum '{t}' allocated {n} but only {len(by_type[t])} rows "
                    f"available after redistribution; this indicates a bug."
                )
            sampled.extend(random.sample(by_type[t], n))

    # Add index for traceability
    for i, r in enumerate(sampled, start=1):
        r["index"] = i

    zero_alloc = sorted(t for t, n in allocations.items() if n == 0)
    if zero_alloc:
        print(f"WARNING: {len(zero_alloc)} stratum/strata excluded by Hamilton allocation: {zero_alloc}", file=sys.stderr)

    print(f"Population: {total}, target: {target_n}, actual: {len(sampled)}\n")
    for t, c in type_counts.most_common():
        n = allocations.get(t, 0)
        print(f"  {t:30s} {n:4d}  pop_share={100*c/total:5.1f}%")

    with open(out_path, "w") as f:
        json.dump(sampled, f, indent=2, default=str)
    print(f"\nSaved to {out_path}")


if __name__ == "__main__":
    main()
