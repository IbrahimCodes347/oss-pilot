# relay-lab

A small personal sandbox for playing with GitHub Actions: scheduled runs,
`workflow_dispatch` chaining, and keeping "state in git" so nothing is lost
when a run dies on a free tier runner.

## What's inside

- `.github/workflows/` — a couple of experiments:
  - `controller.yml`: a long-running-ish scheduled job (10-min cadence, manual
    dispatch too) that runs a small script and commits whatever it produced.
  - `gate-poll.yml`: a 5-min poller that reads a Telegram bot's button callbacks
    and materialises tiny JSON "decision" files into `data/`, then commits them.
- `beacon/` — the scripts those workflows call (`gate_poller`, `tick`,
  `wake_controller`, assorted stdlib helpers). Nothing exotic; plain urllib,
  no framework.
- `fixer/` — a vendored copy of a CLI I keep in sync from another project.
- `data/` — the committed working state: offsets, a small board, decision files.
- `config/targets.json` — a watchlist of repos; the scripts skip entries that
  already have open work to avoid piling up.

## Why public

Free tier Actions minutes are unlimited on public repos, and this is
experimental junk — feels wasteful to spend a private-repo budget on it.

## Notes

- State lives in git history: each run commits `data/` and any logs, so a
  crashed runner leaves a recoverable trail.
- Decent for learning how `concurrency`, retry-push loops, and internals like
  `GITHUB_TOKEN` vs a personal PAT behave in the wild.