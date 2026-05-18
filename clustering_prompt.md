# Emergent Taxonomy Clustering Prompt (v1)

You are reading 250 structured analyses of escalated chatbot conversations. Each has user_intent, where_bot_got_stuck, what_agent_did, summary, complexity, cluster_hint, and subcluster_hint fields. Your job: propose a two-tier taxonomy that groups similar escalations together for prioritization.

This is **emergent clustering**. Do not assume any specific number of clusters or subclusters. Let the data tell you. Some signals will fold neatly; some will be loose; some will be irreducibly singular.

## The four principles (binding)

1. **Minimum cluster size.** Every subcluster must contain at least 3 records. Singletons and pairs are noise. Fold them up to the parent cluster as "(other)" or merge into a related subcluster.

2. **Distinguishability by action required.** Two clusters must differ in what someone has to BUILD or DO to address them. If "missing carrier rule for Liberty Mutual late fees" and "missing carrier rule for Progressive late fees" both resolve by adding a knowledge-repo entry, they're the same cluster. If they require different fixes (e.g., a new tool vs. a new prompt rule vs. a knowledge entry), they're different clusters.

3. **Meaningful share.** A tier-1 cluster covering less than ~2% of escalations (i.e., fewer than 5 records out of 250) should be merged into a sibling or relegated to a "long tail" bucket.

4. **Coherence within cluster.** Records grouped together should have similar `what_agent_did` values. If half the records require a CRM action and half require a carrier callout, they're two clusters even if user_intent looks similar.

## Your output

Produce a JSON object with this structure:

```json
{
  "taxonomy": [
    {
      "cluster": "<broad name, 1-3 words, e.g. 'Endorsement', 'Payment', 'Carrier callout', 'Document review'>",
      "description": "<1-2 sentences on what defines membership in this cluster>",
      "subclusters": [
        {
          "subcluster": "<specific name, 2-5 words>",
          "description": "<1 sentence on what distinguishes this sub-type>",
          "fix_signature": "<1-2 sentences on what someone would need to build/change to address this. Cite the specific gap: missing tool, missing knowledge entry, missing carrier integration, requires licensed judgment, requires manual CRM action, etc.>",
          "indices": [<list of integer record indices that belong here>]
        }
      ]
    }
  ],
  "long_tail": {
    "description": "Records that did not fit any subcluster meeting all four principles.",
    "indices": [<list of integer record indices>]
  },
  "design_notes": [
    "<1-3 short notes on tradeoffs you made: ambiguous boundaries, principles that conflicted, judgment calls.>"
  ]
}
```

## Hard rules

- Every record's index appears exactly once across all subclusters and the long_tail. Verify before emitting.
- Every subcluster has >= 3 records. If you have 1-2 leftovers in a subcluster, merge or move to long_tail.
- Every cluster has >= 5 total records (across its subclusters). Merge tier-1 clusters that fall below.
- `fix_signature` is the most important field. Be precise. "Add tool" is not enough — say what tool and what it would do.
- Do not use the existing `cluster_hint` and `subcluster_hint` from the analyst output as your taxonomy directly. Use them as input signal, then reorganize emergently around the four principles.

## Input

You will receive a JSON array of 250 analyst result records. Cluster all 250.
