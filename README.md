# oss-pilot

Fully cloud-hosted scheduler for the [issue hunter](https://github.com/IbrahimCodes347/ISSUE-HUNTER) + auto-fixer
(`oss_agent_v2.py`). Everything runs on GitHub Actions free minutes — no laptop,
no server, no Always-On. The 3 GitHub Actions free tiers cover unlimited compute
on public repos, which is why this repo is public.

## How the machine works

- **`controller.yml`** (every 10 min + manual dispatch) runs `beacon/tick.py`:
  1. if an operator already tapped Approve/Decline in Telegram, apply that
     decree with the fixer (`--gate-sync`);
  2. else, if under the daily PR / pending-gate budget, hunt a fresh candidate
     from `config/targets.json` and run a solve;
  3. commit everything (`data/`, `fixer/.agent_data/`) back to git — the git
     history IS the state, so nothing is lost if a run dies.
- **`gate-poll.yml`** (every 5 min) runs `beacon/gate_poller.py`: it pumps the
  Telegram bot, turns button taps into `.decree` files, and commits them.
- The fixer's three human gates (draft-PR, finalize, close) run in **cloud
  mode** (`GATE_ASYNC=1`, no TTY): they park, notify Telegram with
  **Approve/Decline** buttons, and are resumed by the next controller tick.
- One unit of work per tick keeps LLM spend bounded and predictable.

## First-run setup

Create the repo and push this tree, then set secrets:

```
gh secret set GH_PAT -R <owner>/oss-pilot
gh secret set LLM_API_KEY -R <owner>/oss-pilot
gh secret set OMNIROUTE_BASE_URL -R <owner>/oss-pilot
gh secret set OMNIROUTE_MODEL -R <owner>/oss-pilot
gh secret set OMNIROUTE_MODEL_FALLBACKS -R <owner>/oss-pilot
gh secret set TELEGRAM_BOT_TOKEN -R <owner>/oss-pilot
gh secret set TELEGRAM_CHAT_ID -R <owner>/oss-pilot
gh secret set SIGNOFF_NAME -R <owner>/oss-pilot
gh secret set SIGNOFF_EMAIL -R <owner>/oss-pilot
```

| Secret | Meaning |
| --- | --- |
| `GH_PAT` | classic PAT, scopes `repo` (+ `workflow` if tweaking workflows). Used for all GitHub + git auth. A `GITHUB_TOKEN` built into Actions cannot fork/PR on third-party repos, so a real PAT is required. |
| `LLM_API_KEY` | AI provider key. Default: your Gemini key with `OMNIROUTE_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/` and `OMNIROUTE_MODEL=gemini-2.0-flash` (free tier). OpenRouter also works: base `https://openrouter.ai/api/v1`, model `google/gemini-2.0-flash-exp:free`. |
| `OMNIROUTE_MODEL_FALLBACKS` | comma list tried in order on quota/rate-limit, e.g. `gemini-2.0-flash-lite`. |
| `TELEGRAM_BOT_TOKEN` | bot from @BotFather (the one already wired for `Hunter_kill_bot`). |
| `TELEGRAM_CHAT_ID` | chat id where Approve/Decline buttons appear (your `7128424097`). |
| `SIGNOFF_NAME` / `SIGNOFF_EMAIL` | DCO sign-off identity (must match the PAT account). |

**Keep the `.env` values out of git** — they go straight into repo secrets.

## Configuration

`config/targets.json`:

- `max_prs_per_day` — hard ceiling on PRs opened per day (default 2).
- `max_pending_gates` — how many solves may sit waiting on a human before the
  controller stops starting new ones (default 3).
- `max_stars` — skip repos above this star count (default 3000).
- `targets[]` — one `{ "repo", "labels", "enabled" }` per repo.

## Monitoring

- Telegram: every gate press produces an ack message; every produced decree is
  logged back to the chat.
- `data/log.txt` + `data/board.json` are committed every tick — the board shows
  daily PR count, lanes (per-issue fixer state), and the last tick timestamp.
- Fixer transcripts: `fixer/.agent_data/logs/*.log`, committed every controller
  run (workspace clones are gitignored).

## Known trade-offs (v1)

- Re-solving: a parked human gate stores no fix; the controller re-runs the
  solve when the decree arrives. Costs a few extra free-tier calls per approval.
- Maintainer conversation rounds (post-PR) are not auto-driven yet; a future
  `conversation.yml` will poll draft-PR feedback and run `-conversation`.
- Rotate `GH_PAT` and the API keys if their `.env` plaintext copy ever leaked.