---
name: escalation-clusters
description: Cluster escalated chatbot conversations by reason and prioritize automation opportunities. Pulls a stratified random sample for any date range, runs analyst and classifier pipelines, and writes per-week metrics to a master tracker sheet (cluster shares, subcluster shares, implied uplift, latest run findings). Also supports `/escalation-clusters review` to build a per-week review tab from toggled-on subclusters in the Review queue.
user-invocable: true
argument-hint: "<start_date> <end_date> [n] | review"
---

# Escalation Clusters

Two modes:

- **Default (weekly run):** `/escalation-clusters <start_date> <end_date> [n]` — pulls a stratified sample, runs the analyst + classifier pipelines, computes self-serve / escalation rate via Card 14466 fork, and appends a new column to the master tracker sheet. Overwrites the Latest run findings tab.
- **Review:** `/escalation-clusters review` — reads checkboxes in the Review queue tab and creates a `Review YYYY-MM-DD` tab populated from the latest run findings (one row per matching conversation).

## Master sheet

Single Google Sheet, indefinite rotation: `https://docs.google.com/spreadsheets/d/1P4vSTMZZsgod6yqhszVZJrP39Lvnd_z14kcniV2E2Lw`. Already shared with the SA. Tabs:

| Tab | Behavior |
|---|---|
| Tracker | Header band + top grid (uplift) + bottom grid (count/share). Toggle B6 sets the trailing-avg window (defaults to 3 weeks). Trailing-avg column always sits one col right of the most recent week. |
| Opportunities | Cumulative per-theme view of prompt-improvement opportunities surfaced by the analyst. One row per theme, sorted by latest-week volume. Includes a `Create ticket?` checkbox that fires the Apps Script Asana trigger. |
| Findings YYYY-MM-DD | One row per conversation for the run, including the per-conv opportunity fields and theme assignment. |
| Taxonomy output YYYY-MM-DD | Per-week cluster + subcluster snapshot with review-queue checkboxes. |
| Week of YYYY-MM-DD | Per-week separator tab (gray). |
| Review YYYY-MM-DD | Per-invocation review tab from `/escalation-clusters review`. |
| Taxonomy | Reference (unchanged). |

## Parameters (default mode)

`<start_date> <end_date> [n]`
- **start_date / end_date** (YYYY-MM-DD, ET): inclusive range
- **n** (default 500): sample size. Recommended 250 for spot-checks, 500 for routine reporting, 1000 for opportunity sizing decisions.

The Sunday cron (registered separately via `/schedule`) fires `/escalation-clusters <last_monday> <last_sunday> 500`.

## Working directory

`/Users/charliejacob/escalation_clusters/runs/<run_id>/` for intermediate files. `<run_id>` defaults to `<start_date>_to_<end_date>_n<n>`. Reuse on re-runs to avoid re-pulling.

## Process (default mode)

### Phase 1: Pull escalation population

```bash
RUN_DIR=/Users/charliejacob/escalation_clusters/runs/${start_date}_to_${end_date}_n${n}
mkdir -p $RUN_DIR
sed "s/{{start_date}}/'$start_date'/g; s/{{end_date}}/'$end_date'/g" \
    ~/.claude/skills/escalation-clusters/population.sql > $RUN_DIR/population.sql
python3 /Users/charliejacob/crm_tag_deep_dive/run_query.py $RUN_DIR/population.sql $RUN_DIR/population.json
```

The Metabase API caps at 2000 rows. If a range has >2000 escalations, the most recent 2000 are returned (acceptable for sampling).

### Phase 2: Self-serve / escalation rate

```bash
sed "s/{{start_date}}/'$start_date'/g; s/{{end_date}}/'$end_date'/g" \
    ~/.claude/skills/escalation-clusters/self_serve_query.sql > $RUN_DIR/self_serve_query.sql
python3 /Users/charliejacob/crm_tag_deep_dive/run_query.py $RUN_DIR/self_serve_query.sql $RUN_DIR/self_serve_result.json
```

This is a fork of Card 14466 (Chatbot Total Messages V4) returning one row: `total_bot_convs`, `escalated_convs`, `self_serve_convs`, `self_serve_rate`, `escalation_rate`. Validated against Card 14466 to the row for week of 2026-04-24 (10,796 / 8,049).

### Phase 3: Stratified proportional sample

```bash
python3 ~/.claude/skills/escalation-clusters/sample.py \
    $RUN_DIR/population.json $RUN_DIR/sample.json $n
```

Print the allocation table.

### Phase 4: Build adapter inputs

```bash
python3 ~/.claude/skills/escalation-clusters/build_inputs.py \
    $RUN_DIR/sample.json $RUN_DIR/propelix_input.json $RUN_DIR/front_input.json
```

### Phase 5: Pull bot + Front data in parallel

```bash
python3 ~/.claude/skills/escalation-clusters/pull_bot_data.py \
    $RUN_DIR/propelix_input.json $RUN_DIR/propelix_output.json &

python3 /Users/charliejacob/.claude/skills/review-escalations/pull_front_batch.py \
    $RUN_DIR/front_input.json $RUN_DIR/front_output.json &

wait
```

### Phase 6: Build enriched records

```bash
python3 ~/.claude/skills/escalation-clusters/build_enriched.py \
    $RUN_DIR/sample.json $RUN_DIR/propelix_output.json $RUN_DIR/front_output.json $RUN_DIR/enriched.json
```

### Phase 7: Analyst pipeline (parallel subagents)

```bash
NUM_BATCHES=$(python3 -c "import math; print(math.ceil($n / 25))")
python3 ~/.claude/skills/escalation-clusters/split_batches.py \
    $RUN_DIR/enriched.json $RUN_DIR/batch $NUM_BATCHES
```

Launch one Sonnet subagent per batch via the Agent tool with `run_in_background: true`. Each agent receives this prompt:

> Run the Escalation Reason Analyst pipeline (v1) on a batch of N chatbot escalation records.
>
> Read these two files:
> 1. `/Users/charliejacob/.claude/skills/escalation-clusters/analyst_prompt.md` — full analyst instructions including bot capabilities/limitations.
> 2. `<RUN_DIR>/batch_<i>.json` — array of N enriched escalation records.
>
> For each record, follow the prompt and emit a JSON object with the exact schema in the prompt: index, display_name, user_intent, where_bot_got_stuck, what_agent_did, summary, complexity, cluster_hint, subcluster_hint.
>
> Cite real evidence from `bot_thread`, `agent_thread`, or `front_signals`. If `agent_thread` is empty or `agent_thread_source: "none"`, say so explicitly in `what_agent_did`. Keep prose to 1-2 sentences per field.
>
> CRITICAL: subcluster_hint must be reusable across multiple conversations.
>
> Write the JSON array to `<RUN_DIR>/results_<i>.json`. Valid JSON only.

Wait for all batches via task notifications. Merge:

```bash
python3 -c "
import json, glob
all_r = []
for p in sorted(glob.glob('$RUN_DIR/results_*.json')):
    all_r.extend(json.load(open(p)))
all_r.sort(key=lambda r: r.get('index', 0))
json.dump(all_r, open('$RUN_DIR/analyst_merged.json', 'w'), indent=2, ensure_ascii=False)
print(f'Merged {len(all_r)} records')
"
```

### Phase 8: Classifier pipeline

Split into batches of 50:

```bash
NUM_CLS=$(python3 -c "import math; print(math.ceil($n / 50))")
python3 ~/.claude/skills/escalation-clusters/split_batches.py \
    $RUN_DIR/analyst_merged.json $RUN_DIR/cls_batch $NUM_CLS
```

Launch one Sonnet subagent per classifier batch:

> Run the Escalation Classifier pipeline (v1).
>
> Read:
> 1. `/Users/charliejacob/.claude/skills/escalation-clusters/classifier_prompt.md`
> 2. `/Users/charliejacob/.claude/skills/escalation-clusters/taxonomy.json`
> 3. `<RUN_DIR>/cls_batch_<i>.json`
>
> For each record, assign exactly one (cluster, subcluster) from the taxonomy or mark `unclassified`. Output the schema in classifier_prompt.md.
>
> Write the JSON array to `<RUN_DIR>/cls_results_<i>.json`. Valid JSON only.

If a classifier batch fails with rate-limit (e.g., usage limit), retry the same batch once before continuing. Merge:

```bash
python3 -c "
import json, glob
all_r = []
for p in sorted(glob.glob('$RUN_DIR/cls_results_*.json')):
    all_r.extend(json.load(open(p)))
all_r.sort(key=lambda r: r.get('index', 0))
json.dump(all_r, open('$RUN_DIR/classifier_merged.json', 'w'), indent=2, ensure_ascii=False)
"
```

### Phase 8.5: Opportunity theme merger

Pull this week's per-conversation opportunities (analyst output where `prompt_opportunity.type != 'none'`) plus existing themes from the Opportunities tab, then run a Sonnet subagent to merge.

```bash
python3 ~/.claude/skills/escalation-clusters/prep_opportunities.py \
    "https://docs.google.com/spreadsheets/d/1P4vSTMZZsgod6yqhszVZJrP39Lvnd_z14kcniV2E2Lw/edit" \
    $RUN_DIR/analyst_merged.json $RUN_DIR/classifier_merged.json \
    $RUN_DIR/opps_to_merge.json $RUN_DIR/existing_themes.json
```

Launch ONE Sonnet subagent with this prompt:

> Run the Opportunity Theme Merger (v1).
>
> Read:
> 1. `/Users/charliejacob/.claude/skills/escalation-clusters/clustering_opportunities_prompt.md`
> 2. `<RUN_DIR>/opps_to_merge.json`
> 3. `<RUN_DIR>/existing_themes.json`
>
> For each opportunity, decide `mapped_existing`, `new_theme`, or `skipped` per the prompt's rules. Emit the schema described in clustering_opportunities_prompt.md.
>
> Write the JSON object to `<RUN_DIR>/theme_merge_output.json`. Valid JSON only.

If `opps_to_merge.json` is empty (no opportunities this week — small samples or runs where every escalation was a capability gap), skip the subagent and write `{"per_opportunity": [], "new_themes": []}` directly.

### Phase 9: Build findings + summaries

```bash
ESC_RATE=$(python3 -c "import json; print(json.load(open('$RUN_DIR/self_serve_result.json'))[0]['escalation_rate'])")
python3 ~/.claude/skills/escalation-clusters/build_findings.py \
    $RUN_DIR/enriched.json $RUN_DIR/analyst_merged.json $RUN_DIR/classifier_merged.json \
    ~/.claude/skills/escalation-clusters/taxonomy.json $RUN_DIR/output $ESC_RATE \
    $RUN_DIR/theme_merge_output.json
```

If unclassified rate >5%, flag to Charlie before writing — the taxonomy may need rebuild (see "Rebuild taxonomy").

### Phase 10: Write to master sheet

```bash
WEEK_LABEL="Week of $start_date"
python3 ~/.claude/skills/escalation-clusters/master_sheet_writer.py \
    "https://docs.google.com/spreadsheets/d/1P4vSTMZZsgod6yqhszVZJrP39Lvnd_z14kcniV2E2Lw/edit" \
    "$WEEK_LABEL" \
    $RUN_DIR/output/findings.json \
    $RUN_DIR/output/cluster_summary.json \
    $RUN_DIR/output/subcluster_summary.json \
    $RUN_DIR/self_serve_result.json
```

This appends a new week column to the Tracker (top + bottom grids), recomputes the trailing-avg column on the right, and writes the per-week Findings + Taxonomy output tabs.

### Phase 10.5: Update cumulative Opportunities tab

```bash
python3 ~/.claude/skills/escalation-clusters/write_opportunities.py \
    "https://docs.google.com/spreadsheets/d/1P4vSTMZZsgod6yqhszVZJrP39Lvnd_z14kcniV2E2Lw/edit" \
    "$start_date" \
    $RUN_DIR/output/findings.json \
    $RUN_DIR/theme_merge_output.json \
    /Users/charliejacob/escalation_clusters/opportunities_state.json
```

Reads existing theme metadata from the Opportunities tab (source of truth for theme names + user edits like Status / Asana ticket / Create ticket?), merges new themes from the subagent output, increments weekly volume + samples in the sidecar state file, then re-renders the tab sorted by latest-week volume desc.

### Phase 11: Notify

For cron runs, post a Slack DM to Charlie (`U09AH1YD6CF`) with a one-paragraph summary: SS rate, top 3 clusters by uplift, sheet URL.

For manual runs, present the same summary in chat.

## Process (review mode)

```bash
python3 ~/.claude/skills/escalation-clusters/review_sheet_builder.py \
    "https://docs.google.com/spreadsheets/d/1P4vSTMZZsgod6yqhszVZJrP39Lvnd_z14kcniV2E2Lw/edit"
```

Reads checked rows in Review queue, finds matching conversations in Latest run findings, creates `Review YYYY-MM-DD` tab. Overwrites if it already exists for today's date.

If no checkboxes are toggled, the script no-ops with a message.

## One-time bootstrap

`init_master_sheet.py` was run once on 2026-05-06 to bootstrap the master sheet structure. Only re-run if the taxonomy changes substantially or the sheet needs to be rebuilt from scratch.

```bash
python3 ~/.claude/skills/escalation-clusters/init_master_sheet.py \
    "https://docs.google.com/spreadsheets/d/1P4vSTMZZsgod6yqhszVZJrP39Lvnd_z14kcniV2E2Lw/edit" \
    ~/.claude/skills/escalation-clusters/taxonomy.json \
    /path/to/subcluster_summary.json
```

## Rebuild taxonomy

When unclassified rate >5% or quarterly review is due:

1. Run Phases 1-7 on a fresh sample (n=300+).
2. Skip Phase 8 (classifier).
3. Launch TWO Sonnet subagents in parallel running `clustering_prompt.md`. Save to `$RUN_DIR/taxonomy_a.json` and `$RUN_DIR/taxonomy_b.json`.
4. Compare and reconcile into `taxonomy_v2.json`.
5. After Charlie locks v2, copy to `~/.claude/skills/escalation-clusters/taxonomy.json` and re-run `init_master_sheet.py` to refresh Review queue rows.

## Files

| File | Purpose |
|------|---------|
| `population.sql` | Population query (escalations only) |
| `self_serve_query.sql` | Self-serve / escalation rate (Card 14466 fork) |
| `analyst_prompt.md` | Analyst instructions (now includes prompt_opportunity field) |
| `classifier_prompt.md` | Classifier instructions |
| `clustering_prompt.md` | Emergent taxonomy clustering (rebuild only) |
| `clustering_opportunities_prompt.md` | Per-run opportunity theme merger |
| `taxonomy.json` | Locked taxonomy v1 (12 clusters / 46 subclusters) |
| `pull_bot_data.py` | Propelix CLI puller |
| `sample.py` | Proportional stratified sampler |
| `build_inputs.py` | Adapter for bot + Front pull inputs |
| `build_enriched.py` | Merger for sample + bot + Front |
| `split_batches.py` | Splits records into N batches |
| `build_findings.py` | Final tables: findings, cluster summary, subcluster summary |
| `prep_opportunities.py` | Builds merger inputs (opps_to_merge, existing_themes) |
| `write_opportunities.py` | Applies merger output to cumulative Opportunities tab + state file |
| `master_sheet_writer.py` | Per-run master sheet writer (Tracker, Findings, Taxonomy output) |
| `review_sheet_builder.py` | Per-week review tab builder (review mode) |
| `init_master_sheet.py` | One-time master sheet bootstrap |
| `opportunity_asana_button.gs` | Apps Script for the Opportunities tab Asana ticket button |
| `write_to_sheet.py` | Legacy v1 writer (kept for reference) |

External dependencies:
- `/Users/charliejacob/crm_tag_deep_dive/run_query.py` — Metabase API runner (DB 8)
- `/Users/charliejacob/.claude/skills/review-escalations/pull_front_batch.py` — Front API puller
- `/Users/charliejacob/.claude/google-sheets-sa.json` — service account credentials

## Corrections to remember

- Service account `claude-sheets@molten-album-490719-s4.iam.gserviceaccount.com` has 0 Drive storage. Cannot create new Sheets.
- Metabase API caps at 2000 rows. For n>2000 escalations the most recent 2000 are returned.
- `magic.mv_user_stage_history` populates user_stage at exact escalation timestamp via per-row JOIN. Coarse signal (PH vs in-quoting vs post-quoting) is reliable; exact stage isn't.
- `is_policyholder` from `magic.mv_policy_status` with `start_date < end_date AND end_date > toDate(escalation_time)`.
- Self-serve / escalation-rate definition: Card 14466 (Chatbot Total Messages V4). Forked into `self_serve_query.sql`. Validated to the row.
- Self-serve exclusions: Postpone Payment, Pre Cancel Direct, REQUEST_URGENT_SWITCH (matches Card 14466's exclusion set).
- Run `/check-sql` on any new SQL before executing.
- `pull_bot_data.py` returns chronological threads with JSON tool stripping and inline agent fallback. Don't switch back to `pull_propelix_batch.py`.
- Population SQL has three-tier conv_id resolution. Phone-path conv_ids work via primary `target_msg_id` join.

## Apps Script (Asana ticket button) — one-time install

The Opportunities tab has a `Create ticket?` checkbox per row. Flipping it to TRUE auto-creates an Asana task in the Prompt Updates section of the Chatbot Team project, assigned to Charlie, and writes the permalink back to the row. Setup is one-time per sheet:

1. Open the master sheet > Extensions > Apps Script.
2. Replace `Code.gs` with the contents of `~/.claude/skills/escalation-clusters/opportunity_asana_button.gs`. Save.
3. Project Settings (gear icon) > Script Properties > Add property: key = `ASANA_PAT`, value = the PAT from `~/.claude/asana-pat.txt`. Save.
4. In the script editor, select function `installTriggers` from the dropdown and click Run. Authorize when prompted (the script needs Sheets + UrlFetchApp access).
5. Done. Flipping the `Create ticket?` checkbox on any Opportunities row will now create an Asana task. The checkbox rolls back to FALSE if the API call errors so you can retry after fixing the cause.

Default routing: project `1201602424812416` (Chatbot Team), section `1204515908618145` (Prompt Updates), assignee `1211055619190425` (Charlie). Reassign in Asana after filing.

## Status

- v1 shipped 2026-05-04 (n=500 sample on week of 2026-04-24, taxonomy locked).
- v2 master tracker shipped 2026-05-06: self-serve query forked from Card 14466, master sheet bootstrapped, master_sheet_writer + review_sheet_builder operational.
- v3 shipped 2026-05-17: trailing-avg toggle on Tracker (cell B6); analyst prompt_opportunity field; opportunity theme merger pipeline; cumulative Opportunities tab; Apps Script Asana button.
- Sunday 6am ET cron registered separately via `/schedule`.
