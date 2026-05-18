# Escalation Classifier Prompt (v1)

You are classifying escalated chatbot conversations against a locked two-tier taxonomy. For each record, assign it to exactly one (cluster, subcluster) pair from the taxonomy, OR mark it `unclassified` if it doesn't fit any subcluster well.

This is **not** emergent clustering. The taxonomy is fixed. Your job is to map each record to the right slot.

## How to decide

For each record, read the analyst output (`user_intent`, `where_bot_got_stuck`, `what_agent_did`, `summary`, `complexity`) and compare against each subcluster's `subcluster` name and `fix_signature`. The fix_signature describes what someone would need to BUILD or DO to resolve cases of that type. Match on action required, not on surface user intent.

Examples:
- A user asking to add a vehicle whose endorsement is blocked by missing UW info goes to `New policy bind > Carrier-required bind UW info collection` (fix = bot-side UW collector), not `Endorsement execution > Add/swap/remove vehicle`, because the actual blocker is UW data, not the endorsement tool.
- A user asking why their renewal premium went up goes to `Knowledge / advisory only` (fix = expand knowledge repo) if the resolution is "agent explained the carrier's renewal rules," but goes to `Billing dispute > Premium / installment / renewal increase explanation` (fix = build a bot-side renewal-billing explainer) if the resolution required carrier-side investigation.

## When to mark unclassified

Use `unclassified` ONLY when:
- No subcluster's fix_signature matches the record's required action, AND
- Forcing it into the closest subcluster would make that subcluster less coherent

If you're tempted to use unclassified because you're unsure between two subclusters, pick the better fit and set `confidence: medium` instead. Reserve unclassified for genuinely novel patterns the taxonomy doesn't cover.

If unclassified rate goes above ~5% in a run, that's a signal to update the taxonomy (run `clustering_prompt.md` on the unclassified bucket).

## Output schema

For each record, emit a JSON object:

```json
{
  "index": <int>,
  "cluster": "<exact cluster name from taxonomy, or 'unclassified'>",
  "subcluster": "<exact subcluster name from taxonomy, or 'unclassified'>",
  "confidence": "<high | medium | low>",
  "rationale": "<1 sentence: which fix_signature this record matches and why>"
}
```

## Hard rules

1. `cluster` and `subcluster` must EXACTLY match strings in the taxonomy file. No paraphrasing, no normalization. If you cannot match exactly, set both to `unclassified`.
2. `confidence`: `high` if the fix_signature clearly applies and the record's `what_agent_did` aligns. `medium` if the fix_signature applies but ambiguity exists between this and one other subcluster. `low` if you're not sure.
3. Do NOT invent new clusters or subclusters. The taxonomy is the closed set.
4. `rationale` is one sentence. Cite the matching fix_signature concept (e.g., "missing carrier-UW-info collector tool"), not the user's surface intent.

## Inputs

You will receive:
1. The taxonomy as a JSON object with `taxonomy[]` of clusters, each containing `subclusters[]` with `subcluster` names and `fix_signature` text.
2. A batch of analyst result records with the schema above.

Return a JSON array of classification objects, one per record, in the same order as input.
