"""Candidate hunter: pick the next (repo, issue) to attempt this tick.

Pure stdout-free, stdlib-only GitHub REST. This is a PRE-FILTER only -- the
vendor fixer re-validates everything itself (classification, difficulty,
report-only routing, PR limits). The hunter just avoids obviously-wrong
targets (already attempted, hard-labelled, too many stars, locked, assigned,
already has one of our open PRs) and lets the fixer make the final call.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import beacon_util as util  # noqa: E402

# Labels that signal a much-bigger-than-bugfix task. The fixer has its own
# difficulty scoring; this blocklist just saves an LLM-solve on obvious no-gos.
_HARD_HINTS = ("hard", "advanced", "complex", "epic", "major", "big", "won't fix")


def _is_hard(issue: dict) -> bool:
    for label in issue.get("labels", []) or []:
        name = str(label.get("name", "")).lower()
        if any(h in name for h in _HARD_HINTS):
            return True
    return False


def _eligible(issue: dict, attempts: set) -> bool:
    if issue.get("pull_request"):
        return False
    if issue.get("locked"):
        return False
    if issue.get("assignees"):
        return False
    if issue.get("state") != "open":
        return False
    if issue.get("number") in attempts:
        return False
    return not _is_hard(issue)


def _stars_ok(repo_full: str, max_stars: int) -> bool:
    if max_stars <= 0:
        return True
    meta = util.gh_api("GET", f"/repos/{repo_full}")
    if not meta:
        return True  # let the fixer decide when the meta call fails
    return int(meta.get("stargazers_count", 0)) <= max_stars


def _open_pr_count(repo_full: str, login: str) -> int:
    pulls = util.gh_paged(f"/repos/{repo_full}/pulls?state=open")
    return sum(1 for pr in pulls if (pr.get("user") or {}).get("login") == login)


def find_candidate(conf: dict, board: dict) -> tuple | None:
    """Return (repo_full_name, issue_number) or None if nothing is worth a try."""
    login = (util.gh_api("GET", "/user") or {}).get("login", "")
    max_stars = int(conf.get("max_stars", 3000))
    labels = conf.get("default_labels", ["good first issue"])

    targets = [t for t in conf.get("targets", []) if t.get("enabled", True)]
    if not targets:
        return None

    for target in targets:
        repo_full = target["repo"]
        attempts = set(board.get("attempted", {}).get(repo_full, []))
        if not _stars_ok(repo_full, max_stars):
            util.log(f"{repo_full}: skipping (stars > {max_stars})")
            continue
        if login and _open_pr_count(repo_full, login) >= 1:
            util.log(f"{repo_full}: skipping (we already have an open PR there)")
            continue

        repo_labels = target.get("labels") or labels
        for label in repo_labels:
            path = f"/repos/{repo_full}/issues?state=open&labels={label}"
            issues = util.gh_paged(path)
            for issue in issues:
                num = issue.get("number")
                if num is None:
                    continue
                if _eligible(issue, attempts):
                    return repo_full, num
            util.log(f"{repo_full}: no eligible issue under label '{label}'")
    return None