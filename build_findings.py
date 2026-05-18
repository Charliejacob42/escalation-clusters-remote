#!/usr/bin/env python3
"""Merge analyst results + classifier assignments + enriched data into Findings rows.

Usage: build_findings.py <enriched.json> <analyst_merged.json> <classifier_merged.json> <taxonomy.json> <output_dir> [esc_rate]
Outputs three files in output_dir: findings.json, cluster_summary.json, subcluster_summary.json
"""
import json
import os
import sys
from collections import defaultdict


def main():
    if len(sys.argv) < 6:
        print("Usage: build_findings.py <enriched.json> <analyst.json> <classifier.json> <taxonomy.json> <output_dir> [esc_rate] [theme_merge_output.json]", file=sys.stderr)
        sys.exit(1)
    enr_path, an_path, cls_path, tax_path, out_dir = sys.argv[1:6]
    esc_rate = float(sys.argv[6]) if len(sys.argv) > 6 else 0.25
    merge_path = sys.argv[7] if len(sys.argv) > 7 else None
    os.makedirs(out_dir, exist_ok=True)

    with open(enr_path) as f: enriched = json.load(f)
    with open(an_path) as f: analyst = json.load(f)
    with open(cls_path) as f: cls_results = json.load(f)
    with open(tax_path) as f: tax = json.load(f)

    # Optional: per-conversation theme assignment from the opportunity merger.
    theme_by_idx = {}
    if merge_path and os.path.exists(merge_path):
        with open(merge_path) as f: merge = json.load(f)
        for po in merge.get("per_opportunity", []):
            if po.get("theme_id") and po.get("decision") in ("mapped_existing", "new_theme"):
                theme_by_idx[po["index"]] = po["theme_id"]

    enr_by = {r["index"]: r for r in enriched}
    an_by = {r["index"]: r for r in analyst}
    cls_by = {r["index"]: r for r in cls_results}

    # Coverage validation: every enriched record should be present in both analyst
    # and classifier outputs. Missing entries silently yield empty findings fields.
    missing_analyst = sorted(set(enr_by.keys()) - set(an_by.keys()))
    if missing_analyst:
        sample = missing_analyst[:10]
        suffix = f" (+ {len(missing_analyst)-10} more)" if len(missing_analyst) > 10 else ""
        print(f"WARNING: {len(missing_analyst)} indices missing from analyst output: {sample}{suffix}", file=sys.stderr)
    missing_classifier = sorted(set(enr_by.keys()) - set(cls_by.keys()))
    if missing_classifier:
        sample = missing_classifier[:10]
        suffix = f" (+ {len(missing_classifier)-10} more)" if len(missing_classifier) > 10 else ""
        print(f"WARNING: {len(missing_classifier)} indices missing from classifier output: {sample}{suffix}", file=sys.stderr)

    # subcluster -> fix_signature lookup
    fix_by = {}
    for c in tax["taxonomy"]:
        for s in c["subclusters"]:
            fix_by[(c["cluster"], s["subcluster"])] = s.get("fix_signature", "")

    findings = []
    for i in sorted(enr_by):
        e = enr_by[i]
        a = an_by.get(i, {})
        c = cls_by.get(i, {})
        cluster = c.get("cluster", "unclassified")
        subcluster = c.get("subcluster", "unclassified")
        opp = a.get("prompt_opportunity") or {}
        if not isinstance(opp, dict):
            opp = {}
        findings.append({
            "index": i,
            "display_name": e.get("display_name", ""),
            "propelix_link": e.get("propelix_link", ""),
            "front_link": e.get("front_link", ""),
            "crm_link": e.get("crm_link", ""),
            "escalation_summary": a.get("summary", ""),
            "user_intent": a.get("user_intent", ""),
            "where_bot_got_stuck": a.get("where_bot_got_stuck", ""),
            "what_agent_did": a.get("what_agent_did", ""),
            "complexity": a.get("complexity", ""),
            "escalation_type": e.get("escalation_type", ""),
            "team": e.get("team", ""),
            "channel": e.get("channel", ""),
            "user_stage_id": e.get("user_stage_id"),
            "user_stage_label": e.get("user_stage_label", ""),
            "is_policyholder": e.get("is_policyholder"),
            "escalation_timestamp": e.get("escalation_timestamp", ""),
            "cluster": cluster,
            "subcluster": subcluster,
            "classifier_confidence": c.get("confidence", ""),
            "classifier_rationale": c.get("rationale", ""),
            "fix_signature": fix_by.get((cluster, subcluster), ""),
            "opportunity_type": opp.get("type", "none"),
            "opportunity_rule_text": opp.get("rule_text", ""),
            "opportunity_suggested_change": opp.get("suggested_change", ""),
            "opportunity_evidence_quote": opp.get("evidence_quote", ""),
            "opportunity_theme_id": theme_by_idx.get(i, ""),
        })

    # Cluster + subcluster summary
    cluster_records = defaultdict(list)
    sub_records = defaultdict(list)
    for f in findings:
        cluster_records[f["cluster"]].append(f)
        sub_records[(f["cluster"], f["subcluster"])].append(f)

    SAMPLE_N = len(findings)
    cluster_summary = []
    for c in sorted(cluster_records, key=lambda c: -len(cluster_records[c])):
        n = len(cluster_records[c])
        share = n / SAMPLE_N
        lift_pp = share * esc_rate * 100
        cluster_summary.append({
            "cluster": c,
            "records": n,
            "share_of_sample": share,
            "implied_max_self_serve_lift_pp": lift_pp,
            "subclusters_count": len({f["subcluster"] for f in cluster_records[c]}),
        })

    sub_summary = []
    for tax_c in tax["taxonomy"]:
        for s in tax_c["subclusters"]:
            recs = sub_records[(tax_c["cluster"], s["subcluster"])]
            sub_summary.append({
                "cluster": tax_c["cluster"],
                "subcluster": s["subcluster"],
                "records": len(recs),
                "share_of_sample": len(recs) / SAMPLE_N,
                "fix_signature": s.get("fix_signature", ""),
            })
    # Append unclassified summary if any
    unclassified = [f for f in findings if f["cluster"] == "unclassified"]
    if unclassified:
        sub_summary.append({
            "cluster": "unclassified",
            "subcluster": "unclassified",
            "records": len(unclassified),
            "share_of_sample": len(unclassified) / SAMPLE_N,
            "fix_signature": "Not yet classified. Run clustering pass on these to propose taxonomy additions.",
        })
    sub_summary.sort(key=lambda x: -x["records"])

    # Validation: cluster totals should equal sum of their subcluster records.
    # Allow off-by-one for unclassified edge cases.
    cluster_records_by_name = {c["cluster"]: c["records"] for c in cluster_summary}
    sub_sum_by_cluster = defaultdict(int)
    for s in sub_summary:
        sub_sum_by_cluster[s["cluster"]] += s["records"]
    for cname, ctotal in cluster_records_by_name.items():
        sub_total = sub_sum_by_cluster.get(cname, 0)
        if abs(ctotal - sub_total) > 1:
            print(
                f"WARNING: cluster '{cname}' total ({ctotal}) does not match sum of subcluster records ({sub_total}); diff={ctotal - sub_total}",
                file=sys.stderr,
            )

    # Validation: every subcluster's parent cluster must exist in cluster_summary.
    orphan_subs = [
        (s["cluster"], s["subcluster"])
        for s in sub_summary
        if s["cluster"] not in cluster_records_by_name
    ]
    if orphan_subs:
        print(
            f"WARNING: {len(orphan_subs)} subcluster(s) reference a cluster not in cluster_summary: {orphan_subs[:10]}{' (+ more)' if len(orphan_subs) > 10 else ''}",
            file=sys.stderr,
        )

    # Validation: flag if unclassified share exceeds 5% of the sample.
    if len(findings) > 0 and len(unclassified) > 0.05 * len(findings):
        pct = 100 * len(unclassified) / len(findings)
        print(
            f"WARNING: unclassified records exceed 5% of sample ({len(unclassified)}/{len(findings)} = {pct:.1f}%); consider rerunning the classifier or expanding the taxonomy",
            file=sys.stderr,
        )

    with open(os.path.join(out_dir, "findings.json"), "w") as f:
        json.dump(findings, f, indent=2, default=str)
    with open(os.path.join(out_dir, "cluster_summary.json"), "w") as f:
        json.dump(cluster_summary, f, indent=2, default=str)
    with open(os.path.join(out_dir, "subcluster_summary.json"), "w") as f:
        json.dump(sub_summary, f, indent=2, default=str)

    print(f"  Findings:   {len(findings)} rows")
    print(f"  Clusters:   {len(cluster_summary)}")
    print(f"  Subclusters:{len(sub_summary)}")
    print(f"  Unclassified: {len(unclassified)} ({100*len(unclassified)/SAMPLE_N:.1f}%)")
    print(f"  Implied lift assumes esc_rate={esc_rate}")


if __name__ == "__main__":
    main()
