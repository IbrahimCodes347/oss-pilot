"""Controller tick: one bounded unit of work per invocation.

Decision order per tick:
  1. Apply any waiting gate decree (highest priority -- an operator already
     tapped a button). Runs the fixer with --gate-sync so the decree is
     consumed exactly once and the PR (or decline) becomes real.
  2. Otherwise, if we are below the pending-gate and daily-PR budget, hunt a
     fresh candidate and run a solve; the fixer parks its human gate for the
     operator instead of crashing.
  3. Otherwise just housekeeping.

Only ONE unit per tick, on purpose: the controller cron fires every ~10min,
so the cloud spends a bounded, predictable amount of LLM time. Everything it
produces (board, fixer record, gate files, logs) is committed by the workflow
step that runs this script, which is what makes the whole machine recoverable
from nothing but the git history.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import beacon_util as util  # noqa: E402
import hunter_stage  # noqa: E402

RUN_TIMEOUT_SECONDS = 55 * 60  # Actions default job timeout is generous
PENDING_MAX_AGE_DAYS = 14


def _run_fixer(args: list) -> str:
    cmd = [sys.executable, str(util.FIXER_SCRIPT), *args]
    util.log(f"fixer: {' '.join(cmd)}  (cwd={util.FIXER})")
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(util.FIXER),
            env=util.env_for_fixer(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=RUN_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        util.log("fixer TIMED OUT after %s s" % RUN_TIMEOUT_SECONDS)
        return "timeout"
    tail = ((proc.stdout or "")[-1500:] + "\n" + (proc.stderr or "")[-500:]).strip()
    util.log(f"fixer exit={proc.returncode}\n--tail--\n{tail}")
    return "ok" if proc.returncode == 0 else f"exit{proc.returncode}"


def _workflow_record(repo_name: str, issue_number: int):
    safe = repo_name.replace("/", "-")
    rec = util.FIXER / ".agent_data" / "workflows" / safe / f"issue-{issue_number}.json"
    if not rec.exists():
        return None
    try:
        return util.load_json(rec, None)
    except Exception:
        return None


def _attempted(board, repo_name):
    board.setdefault("attempted", {})
    board["attempted"].setdefault(repo_name, [])
    return board["attempted"][repo_name]


def _pending_keys() -> list:
    if not util.GATES.exists():
        return []
    return sorted(p.stem for p in util.GATES.glob("*.pending"))


def _decree_keys() -> list:
    if not util.GATES.exists():
        return []
    return sorted(p.stem for p in util.GATES.glob("*.decree"))


def _apply_decree(board, key: str) -> bool:
    parsed = util.parse_gate_key(key)
    if not parsed:
        util.log(f"decree {key}: unparseable -- deleting")
        (util.GATES / f"{key}.decree").unlink(missing_ok=True)
        return False
    # The key is lossy for repo names containing hyphens, so trust the rich
    # metadata the poller copied out of the .pending file; the parsed safe
    # name is only a fallback (may be missing the forward slash).
    decree_path = util.GATES / f"{key}.decree"
    meta = util.load_json(decree_path, {})
    repo_name = meta.get("repo") or parsed[0]
    issue_number = meta.get("issue") if meta.get("issue") is not None else parsed[1]
    gate = meta.get("gate") or parsed[2]
    util.log(f"APPLYING decree {key} ({repo_name}#{issue_number}, gate={gate})")
    _run_fixer(["--repo", repo_name, "--issue", str(issue_number), "--gate-sync"])

    if decree_path.exists():
        util.log(f"decree {key} still present: fixer did not reach the gate this run")
        return False

    outcome = util.GATES / f"{key}.outcome"
    decision = None
    if outcome.exists():
        payload = util.load_json(outcome, {})
        decision = bool(payload.get("decision"))
        util.log(f"decree {key} resolved: {'APPROVE' if decision else 'DECLINE'}")

    rec = _workflow_record(repo_name, issue_number) or {}
    if decision:
        board["prs_today"] = int(board.get("prs_today", 0)) + 1
        board.setdefault("prs", []).append({
            "repo": repo_name, "issue": issue_number, "gate": gate,
            "pr": rec.get("pr_number"), "url": rec.get("pr_url"),
            "time": util.now_utc(),
        })
    board.setdefault("lanes", {})[f"{repo_name}#{issue_number}"] = {
        "state": rec.get("state"), "pr": rec.get("pr_number"),
        "gate": gate, "decision": decision, "updated": util.now_utc(),
    }
    return True


def _housekeeping(board) -> None:
    swept = 0
    for pending in util.GATES.glob("*.pending") if util.GATES.exists() else []:
        age_days = (time.time() - pending.stat().st_mtime) / 86400
        if age_days > PENDING_MAX_AGE_DAYS:
            pending.unlink(missing_ok=True)
            swept += 1
    if swept:
        util.log(f"housekeeping: swept {swept} stale .pending file(s)")
    board["last_tick"] = util.now_utc()


def main() -> int:
    util.DATA.mkdir(parents=True, exist_ok=True)
    conf = util.load_json(util.CONFIG, {})
    if not conf:
        util.log("no config/targets.json -- aborting")
        return 1
    board = util.load_json(util.BOARD, {"date": util.today_utc()})
    if board.get("date") != util.today_utc():
        util.log("new day: resetting PR budget")
        board = {"date": util.today_utc(), "prs_today": 0, "prs": [],
                 "attempted": board.get("attempted", {}),
                 "lanes": board.get("lanes", {})}

    acted = False

    decrees = _decree_keys()
    if decrees:
        key = decrees[0]
        _apply_decree(board, key)
        acted = True

    if not acted:
        pending = _pending_keys()
        prs_today = int(board.get("prs_today", 0))
        max_pending = int(conf.get("max_pending_gates", 3))
        max_prs = int(conf.get("max_prs_per_day", 2))
        if len(pending) < max_pending and prs_today < max_prs:
            candidate = hunter_stage.find_candidate(conf, board)
            if candidate:
                repo_name, issue_number = candidate
                util.log(f"HUNTING: trying {repo_name}#{issue_number}")
                _run_fixer(["--repo", repo_name, "--issue", str(issue_number), "--gate-sync"])
                attempts = _attempted(board, repo_name)
                if issue_number not in attempts:
                    attempts.append(issue_number)
                    if len(attempts) > 50:
                        del attempts[: len(attempts) - 50]
                rec = _workflow_record(repo_name, issue_number)
                if rec:
                    board.setdefault("lanes", {})[f"{repo_name}#{issue_number}"] = {
                        "state": rec.get("state"), "pr": rec.get("pr_number"),
                        "updated": util.now_utc(),
                    }
                acted = True
            else:
                util.log("no candidate found this tick")
        else:
            util.log(f"budget: {prs_today}/{max_prs} PRs, {len(pending)}/{max_pending} pending gates")

    _housekeeping(board)
    util.save_json(util.BOARD, board)
    util.log("tick done")
    return 0


if __name__ == "__main__":
    sys.exit(main())