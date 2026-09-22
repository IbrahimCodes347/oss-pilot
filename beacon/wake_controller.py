"""Wake the controller. Runs AFTER the commit step of the gate-poll workflow so
the controller's checkout can never race a not-yet-pushed decree.

The gate_poller itself dispatches during its python step, but the decree file
is only pushed by the workflow's later 'commit state' step -- a controller
that starts in between checks out a tree without the decree and idles. This
step moves the dispatch to after the push and is the single source of truth
for the trigger.
"""
import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import beacon_util as util  # noqa: E402


def dispatch(owner: str, repo: str, workflow: str, ref: str) -> int:
    token = os.getenv("DISPATCH_TOKEN", "") or os.getenv("GITHUB_TOKEN", "")
    if not token:
        util.log("wake: no DISPATCH_TOKEN/GITHUB_TOKEN")
        return 0
    url = (f"https://api.github.com/repos/{owner}/{repo}"
           f"/actions/workflows/{workflow}/dispatches")
    body = json.dumps({"ref": ref}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            util.log(f"wake: dispatched {workflow} (HTTP {resp.status})")
            return 0
    except Exception as exc:
        util.log(f"wake: dispatch {workflow} failed: {exc}")
        return 1


if __name__ == "__main__":
    owner, _, repo = (os.getenv("DISPATCH_REPO", "")).partition("/")
    if not owner or not repo:
        util.log("wake: DISPATCH_REPO not set; nothing to dispatch")
        sys.exit(0)
    sys.exit(dispatch(owner, repo, "controller.yml", "master"))