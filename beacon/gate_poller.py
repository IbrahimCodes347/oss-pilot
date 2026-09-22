"""Gate poller: pumps the Telegram bot for Approve/Decline button presses and
materialises them as `.decree` files under data/gates/.

The vendored fixer weaves its own pump/decree convention:
  data/gates/<owner>-<repo>_issue<N>_<gate>.pending   -> a gate is parked
  data/gates/<owner>-<repo>_issue<N>_<gate>.decree    -> operator decided
    written by THIS poller when a button is tapped; consumed exactly once by
    the fixer on its next run (which then writes a matching .outcome file).

stdlib-only on purpose: this job's whole reason to exist is the 5-minute
cloud poll, so it must not need `pip install`.
"""
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import beacon_util as util  # noqa: E402

OFFSET = util.DATA / "offset.json"


def _tg_call(method: str, payload: dict, timeout: int = 30):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return None
    host = util.TG_API_HOST
    body = json.dumps(payload).encode("utf-8")

    def _post(host: str, with_host_header: bool):
        url = f"https://{host}/bot{token}/{method}"
        headers = {"Content-Type": "application/json"}
        if with_host_header:
            headers["Host"] = util.TG_API_HOST
        req = urllib.request.Request(url, data=body, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            blob = resp.read().decode("utf-8")
            try:
                return json.loads(blob)
            except ValueError:
                return None

    for host, header in [(host, False), *( (ip, True) for ip in util.TG_API_IPS )]:
        try:
            data = _post(host, header)
            if data is not None and data.get("ok") is not False:
                return data
        except Exception:
            continue
    return None


def _get_updates(offset: int):
    payload = {"timeout": 8, "offset": offset, "limit": 20}
    data = _tg_call("getUpdates", payload, timeout=20)
    if not data:
        return []
    return data.get("result") or []


def _answer_callback(callback_id: str, text: str) -> None:
    _tg_call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})


def _notify(text: str) -> None:
    chat = os.getenv("TELEGRAM_CHAT_ID", "")
    if chat:
        _tg_call("sendMessage", {"chat_id": chat, "text": text})


def _handle_callback(query: dict) -> bool:
    data = query.get("data") or ""
    match = re.match(r"^decree:(.+):([01])$", data)
    if not match:
        return False
    key, decision = match.group(1), match.group(2) == "1"
    parsed = util.parse_gate_key(key)

    # Prefer the rich metadata the fixer wrote when it parked the gate; the
    # key itself is lossy for repo names containing hyphens.
    pending = util.GATES / f"{key}.pending"
    meta = util.load_json(pending, {}) if pending.exists() else {}
    repo_name = meta.get("repo") or (parsed[0] if parsed else None)
    issue_number = meta.get("issue") if meta.get("issue") is not None else (parsed[1] if parsed else None)
    gate = meta.get("gate") or (parsed[2] if parsed else None)

    decree = util.GATES / f"{key}.decree"
    decree.write_text(
        json.dumps({
            "key": key,
            "decision": decision,
            "repo": repo_name,
            "issue": issue_number,
            "gate": gate,
            "decided_at": util.now_utc(),
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    if pending.exists():
        pending.unlink()
    label = "APPROVE" if decision else "DECLINE"
    target = f"{repo_name}#{issue_number}" if repo_name else key
    util.log(f"decree written: {target} ({gate or 'gate'}) -> {label}")
    _answer_callback(query.get("id", ""), f"Recorded: {label}")
    _notify(f"Gate {target}: {label} recorded.")
    return True


def main() -> int:
    offset = util.load_json(OFFSET, {}).get("offset", 0)
    util.log(f"gate poller start (offset={offset})")
    handled = 0
    for _ in range(2):  # a couple of passes in case buttons arrive in a burst
        updates = _get_updates(offset)
        if not updates:
            break
        for update in updates:
            update_id = int(update.get("update_id", 0))
            if update_id >= offset:
                offset = update_id + 1
            query = update.get("callback_query")
            if query and _handle_callback(query):
                handled += 1
    if offset:
        util.save_json(OFFSET, {"offset": offset})
    util.log(f"gate poller done: {handled} decree(s), new offset={offset}")
    return 0


if __name__ == "__main__":
    sys.exit(main())