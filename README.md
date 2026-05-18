# escalation-clusters-remote

Private repo backing the **Sunday remote routine** for the `/escalation-clusters` Claude Code skill. The remote agent clones this repo, runs `bootstrap.sh` to materialize credentials from env vars, then follows `SKILL.md` end-to-end.

## Files

- `SKILL.md` — full skill spec (mirror of `~/.claude/skills/escalation-clusters/SKILL.md`)
- `bootstrap.sh` — decodes credential env vars into `~/.claude/...` and `~/.config/propelix/...` paths the skill expects, installs Python deps, verifies Metabase + propelix-cli
- Python scripts + prompt files — full skill bundle (analyst, classifier, opportunity merger, sheet writers)
- `run_query.py` — env-var-aware Metabase client (reads `METABASE_API_KEY` env var; falls back to AWS SSM for local laptop runs)
- `pull_front_batch.py` — Front API puller (lifted from `review-escalations` skill)
- `taxonomy.json` — locked taxonomy v1

## Environment variables (set by the routine prompt)

| Name | Format | Source |
|---|---|---|
| `METABASE_API_KEY` | plain string | Metabase Settings > API Keys |
| `FRONT_API_TOKEN_B64` | base64(token) | local `~/.claude/front-api-token.txt` |
| `PROPELIX_CONFIG_B64` | base64(JSON) | local `~/.config/propelix/config.json` |
| `GOOGLE_SA_B64` | base64(JSON) | local `~/.claude/google-sheets-sa.json` |

## Local development

This repo is the **deployment artifact** for the remote routine. The source of truth for skill logic stays at `~/.claude/skills/escalation-clusters/`. After local changes, mirror them here and push.

## Threat model

Credentials live in the routine prompt config on claude.ai (visible to anyone with access to Charlie's account). Same blast radius as the laptop. Rotate immediately if the claude.ai account is compromised.
