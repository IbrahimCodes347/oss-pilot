"""Persistent, resumable workflow/session storage for oss-agent.

Pure-local and dependency-free on purpose: nothing in this module touches
GitHub, the network, or an LLM. That is the whole safety argument for
``--leave``, ``--status``, ``--list-workflows``, ``--conversation-info`` and
``--finish`` -- they are implemented entirely against this layer, which has no
client with which to change anything remote.

Layout, all under ``AGENT_HOME`` (default ``.agent_data``)::

    state/index.json                        (repo, issue) -> where things live
    workflows/<owner-repo>/issue-<N>.json   authoritative record, atomic writes
    conversations/<owner-repo>/issue-<N>/   metadata.json, transcript.log,
                                            messages.jsonl, archive/<old-id>/
    workspace/<owner-repo>/issue-<N>/       ISOLATED clone, this workflow only
    logs/

Every path is built with pathlib and every directory component is sanitised for
Windows (reserved device names, trailing dots/spaces, illegal characters), so
the same tree works on both platforms. Roots are read from the module global
``AGENT_HOME`` *at call time* so ``configure()`` -- and tests -- can relocate
the whole tree without reimporting.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = 2

AGENT_HOME = Path(".agent_data")


def configure(agent_home) -> None:
    """Point the whole store at `agent_home`. Called by the agent at import and
    by tests; every path helper re-reads the global, so this takes effect
    immediately for code that has already imported this module."""
    global AGENT_HOME
    AGENT_HOME = Path(agent_home)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


# --- errors ---------------------------------------------------------------
class SessionStoreError(RuntimeError):
    """Base class -- always carries a message a user can act on."""


class UnknownWorkflow(SessionStoreError):
    pass


class AmbiguousIssue(SessionStoreError):
    pass


class WorkspaceConflict(SessionStoreError):
    pass


# --- path sanitisation ----------------------------------------------------
# Windows refuses these as file/directory names regardless of extension, and
# silently strips trailing dots and spaces -- which would make two different
# repos collide on one directory. GitHub repo names can't contain most of the
# illegal characters, but a fork owner or a hand-edited record can, so sanitise
# unconditionally rather than trusting the input.
_WIN_RESERVED = {
    "con", "prn", "aux", "nul",
    *{f"com{i}" for i in range(1, 10)},
    *{f"lpt{i}" for i in range(1, 10)},
}
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_component(name: str) -> str:
    """One path component that is legal on both Windows and POSIX."""
    cleaned = _ILLEGAL.sub("-", str(name)).strip().rstrip(". ")
    if not cleaned or cleaned in {".", ".."}:
        cleaned = "_"
    if cleaned.split(".")[0].lower() in _WIN_RESERVED:
        cleaned = f"_{cleaned}"
    return cleaned


def safe_repo_dir(repo_name: str) -> str:
    """``owner/repo`` -> ``owner-repo``, as a single safe component."""
    return _safe_component(str(repo_name).replace("/", "-"))


def issue_dir_name(issue_number) -> str:
    return _safe_component(f"issue-{issue_number}")


# --- roots (re-read AGENT_HOME every call) --------------------------------
def state_dir() -> Path:
    return AGENT_HOME / "state"


def index_path() -> Path:
    return state_dir() / "index.json"


def workflows_root() -> Path:
    return AGENT_HOME / "workflows"


def conversations_root() -> Path:
    return AGENT_HOME / "conversations"


def workspace_root() -> Path:
    return AGENT_HOME / "workspace"


def logs_root() -> Path:
    return AGENT_HOME / "logs"


def workflow_path(repo_name: str, issue_number) -> Path:
    """New nested layout: ``workflows/<owner-repo>/issue-<N>.json``."""
    return workflows_root() / safe_repo_dir(repo_name) / f"{issue_dir_name(issue_number)}.json"


def legacy_workflow_path(repo_name: str, issue_number) -> Path:
    """Where records lived before this module existed: a flat
    ``workflows/<owner-repo>_issue<N>.json``. Still read (see
    ``resolve_workflow_path``) so upgrading never loses a live workflow."""
    return workflows_root() / f"{safe_repo_dir(repo_name)}_issue{issue_number}.json"


def resolve_workflow_path(repo_name: str, issue_number) -> Path:
    """The path to READ. Prefers the new layout, falls back to the legacy flat
    file if that is the only one that exists -- so an in-flight PR opened by the
    old code keeps working. Writes always go to the new layout, which is what
    makes the migration happen lazily on the next save."""
    new = workflow_path(repo_name, issue_number)
    if new.exists():
        return new
    old = legacy_workflow_path(repo_name, issue_number)
    return old if old.exists() else new


def conversation_dir(repo_name: str, issue_number) -> Path:
    return conversations_root() / safe_repo_dir(repo_name) / issue_dir_name(issue_number)


def workspace_dir(repo_name: str, issue_number) -> Path:
    """ISOLATED per (repo, issue). The old code used ``workspace/<repo-name>``
    -- no owner, no issue -- so two workflows shared one clone and
    ``git reset --hard`` in one destroyed the other. Never share this path."""
    return workspace_root() / safe_repo_dir(repo_name) / issue_dir_name(issue_number)


def rel_to_home(path) -> str:
    """Store paths in JSON relative to AGENT_HOME and in posix form, so a state
    tree stays valid if it is moved, and reads the same on both platforms."""
    p = Path(path)
    try:
        return p.resolve().relative_to(Path(AGENT_HOME).resolve()).as_posix()
    except (ValueError, OSError):
        return p.as_posix()


def home_to_abs(rel: str) -> Path:
    p = Path(rel)
    return p if p.is_absolute() else AGENT_HOME / p


# --- atomic IO + advisory locking -----------------------------------------
class FileLock:
    """Cross-platform advisory lock. ``O_CREAT|O_EXCL`` is atomic on NTFS and
    POSIX alike, which is why this is used instead of fcntl/msvcrt. A lock older
    than `stale_after` is broken, so a crashed process cannot wedge the agent
    forever.

    Canonical home for the implementation the agent has always used; re-exported
    as ``oss_agent_v2.FileLock`` with an unchanged signature."""

    def __init__(self, target, timeout=10.0, poll=0.05, stale_after=120.0):
        self.lockfile = Path(str(target) + ".lock")
        self.timeout, self.poll, self.stale_after = timeout, poll, stale_after
        self._fd = None

    def acquire(self):
        deadline = time.time() + self.timeout
        while True:
            try:
                self.lockfile.parent.mkdir(parents=True, exist_ok=True)
                self._fd = os.open(self.lockfile, os.O_CREAT | os.O_EXCL | os.O_RDWR)
                os.write(self._fd, str(os.getpid()).encode())
                return
            except FileExistsError:
                try:
                    if time.time() - self.lockfile.stat().st_mtime > self.stale_after:
                        self.lockfile.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue
                if time.time() >= deadline:
                    raise TimeoutError(
                        f"Could not acquire {self.lockfile} in {self.timeout}s"
                    )
                time.sleep(self.poll)

    def release(self):
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        self.lockfile.unlink(missing_ok=True)

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


def atomic_write_json(path, data) -> None:
    """Write-to-temp-then-``os.replace``, with an fsync in between. A crash
    (or a full disk, or Ctrl+C) leaves the previous file intact rather than a
    half-written one; ``os.replace`` is atomic on Windows too."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, default=str)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def read_json_tolerant(path, default=None, label="file"):
    """Read JSON, quarantining a corrupt file to ``<name>.corrupt`` instead of
    crashing. Returns `default` in that case, so the caller can rebuild. The
    backup is deliberate: a corrupt state file is evidence, not garbage."""
    path = Path(path)
    if not path.exists():
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        backup = path.with_suffix(".corrupt")
        try:
            shutil.copy2(path, backup)
        except OSError:
            backup = None
        where = f" (backed up to {backup})" if backup else ""
        print(f"⚠️  Corrupt {label} at {path}{where}: {exc}")
        print("    Recover with: python oss_agent_v2.py --list-workflows")
        return default


# --- the index: (repo, issue) -> where everything lives -------------------
# This is what lets `--resume 42` work without `--repo`. It is a CACHE, not the
# source of truth: the workflow JSON files are authoritative, and any lookup
# miss triggers a rebuild by scanning them. So deleting index.json is safe.
def index_key(repo_name: str, issue_number) -> str:
    return f"{repo_name}#{int(issue_number)}"


def _empty_index() -> dict:
    return {"schema": SCHEMA_VERSION, "updated_at": now_iso(), "workflows": {}}


def load_index() -> dict:
    idx = read_json_tolerant(index_path(), default=None, label="workflow index")
    if not isinstance(idx, dict) or not isinstance(idx.get("workflows"), dict):
        return _empty_index()
    idx.setdefault("schema", SCHEMA_VERSION)
    return idx


def save_index(idx: dict) -> None:
    idx["updated_at"] = now_iso()
    atomic_write_json(index_path(), idx)


def index_entry_for(record: dict, conversation_id=None) -> dict:
    repo, issue = record["repo"], record["issue"]
    return {
        "repo": repo,
        "issue": issue,
        "issue_title": record.get("issue_title", ""),
        "workflow_path": rel_to_home(resolve_workflow_path(repo, issue)),
        "conversation_dir": rel_to_home(conversation_dir(repo, issue)),
        "workspace": record.get("workspace") or rel_to_home(workspace_dir(repo, issue)),
        "conversation_id": conversation_id or record.get("conversation_id"),
        "state": record.get("state"),
        "status": record.get("status"),
        "branch": record.get("branch"),
        "pr_number": record.get("pr_number"),
        "pr_url": record.get("pr_url"),
        "updated_at": record.get("updated_at") or now_iso(),
    }


def index_upsert(record: dict, conversation_id=None) -> None:
    """Called on every workflow save. Locked, because two repos may legitimately
    run at the same time and both touch this one file."""
    with FileLock(index_path()):
        idx = load_index()
        key = index_key(record["repo"], record["issue"])
        merged = dict(idx["workflows"].get(key, {}))
        merged.update({k: v for k, v in index_entry_for(record, conversation_id).items()
                       if v is not None or k not in merged})
        idx["workflows"][key] = merged
        save_index(idx)


def index_remove(repo_name: str, issue_number) -> bool:
    with FileLock(index_path()):
        idx = load_index()
        gone = idx["workflows"].pop(index_key(repo_name, issue_number), None) is not None
        if gone:
            save_index(idx)
        return gone


def _scan_index_keys() -> set:
    """The (repo, issue) keys that actually exist on disk, in both layouts. Reads
    the records because the filename alone cannot be trusted to reproduce a key
    (``safe_repo_dir`` is deliberately lossy: ``a:b`` and ``a-b`` collapse)."""
    keys = set()
    for path in _scan_workflow_files():
        rec = read_json_tolerant(path, default=None, label="workflow record")
        if not isinstance(rec, dict) or "repo" not in rec or "issue" not in rec:
            continue
        try:
            keys.add(index_key(rec["repo"], rec["issue"]))
        except (TypeError, ValueError):
            continue
    return keys


def index_entries(reconcile: bool = True) -> list:
    """All known workflows, newest first.

    Reconciles against the records on disk by default, because the index is a
    CACHE: a record it has never seen -- a legacy flat file written before this
    module existed, or one restored from a backup -- must not be invisible to
    `--list-workflows`. Rebuilding only when the index was *empty* was not
    enough; one new-layout workflow was sufficient to hide every old one."""
    idx = load_index()
    if reconcile and (_scan_index_keys() - set(idx["workflows"])):
        idx = rebuild_index()
    entries = list(idx["workflows"].values())
    if not entries:
        entries = list(rebuild_index()["workflows"].values())
    entries.sort(key=lambda e: str(e.get("updated_at") or ""), reverse=True)
    return entries


def _scan_workflow_files() -> list:
    """Every workflow record on disk, in BOTH layouts. This scan is the whole
    migration story: old flat records are discovered and indexed automatically,
    so there is no migration command to forget to run."""
    root = workflows_root()
    if not root.exists():
        return []
    found = []
    for path in sorted(root.glob("*/issue-*.json")):     # new nested layout
        found.append(path)
    for path in sorted(root.glob("*_issue*.json")):      # legacy flat layout
        found.append(path)
    return found


def rebuild_index() -> dict:
    """Rebuild from the authoritative records. Safe to call any time; the
    records are never modified, only read."""
    idx = _empty_index()
    for path in _scan_workflow_files():
        rec = read_json_tolerant(path, default=None, label="workflow record")
        if not isinstance(rec, dict) or "repo" not in rec or "issue" not in rec:
            continue
        try:
            key = index_key(rec["repo"], rec["issue"])
        except (TypeError, ValueError):
            continue
        entry = index_entry_for(rec, rec.get("conversation_id"))
        entry["workflow_path"] = rel_to_home(path)
        entry["legacy_layout"] = path.parent == workflows_root()
        prior = idx["workflows"].get(key)
        # New layout wins if both exist for the same workflow.
        if prior is None or prior.get("legacy_layout"):
            idx["workflows"][key] = entry
    try:
        save_index(idx)
    except OSError as exc:
        print(f"⚠️  Could not write {index_path()}: {exc}")
    return idx


def resolve_issue(issue_number, repo_name=None) -> dict:
    """Turn a bare issue number into one index entry.

    With `repo_name` this is an exact lookup. Without it, the number must be
    unambiguous across all tracked repos -- which it usually is, and when it
    isn't the error names every candidate so the user can retry with --repo.
    Never guesses, and never creates anything."""
    issue_number = int(issue_number)
    idx = load_index()
    if not idx["workflows"]:
        idx = rebuild_index()

    if repo_name:
        entry = idx["workflows"].get(index_key(repo_name, issue_number))
        if entry is None:
            idx = rebuild_index()
            entry = idx["workflows"].get(index_key(repo_name, issue_number))
        if entry is None:
            raise UnknownWorkflow(
                f"No saved workflow for {repo_name} issue #{issue_number}.\n"
                f"   Start one with: python oss_agent_v2.py --repo {repo_name} "
                f"--issue {issue_number}\n"
                f"   See what exists: python oss_agent_v2.py --list-workflows"
            )
        return entry

    matches = [e for e in idx["workflows"].values() if int(e.get("issue", -1)) == issue_number]
    if not matches:
        matches = [e for e in rebuild_index()["workflows"].values()
                   if int(e.get("issue", -1)) == issue_number]
    if not matches:
        raise UnknownWorkflow(
            f"No saved workflow for issue #{issue_number} in any tracked repo.\n"
            f"   If this is new work, pass the repo explicitly:\n"
            f"     python oss_agent_v2.py --repo owner/repo --issue {issue_number}\n"
            f"   See what exists: python oss_agent_v2.py --list-workflows"
        )
    if len(matches) > 1:
        repos = ", ".join(sorted(str(m.get("repo")) for m in matches))
        raise AmbiguousIssue(
            f"Issue #{issue_number} exists in {len(matches)} tracked repos: {repos}\n"
            f"   Disambiguate with --repo, e.g.:\n"
            f"     python oss_agent_v2.py --repo {matches[0].get('repo')} "
            f"--issue {issue_number} ..."
        )
    return matches[0]


# --- conversations --------------------------------------------------------
def new_conversation_id() -> str:
    """Sortable, unique, and filesystem-safe."""
    return f"{datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"


class Conversation:
    """The transcript the old code never had.

    ``call_model`` was a single-shot completion with no session object, so every
    invocation started from nothing and "conversation state" existed only in the
    operator's head. This class gives one (repo, issue) a durable identity:

      metadata.json   id, provider/model, counters, archived predecessors
      transcript.log  append-only human-readable log (what --conversation-info
                      shows, and what a person reads after a crash)
      messages.jsonl  machine-readable turns, used to rebuild LLM context

    Append-only by design: a resumed run adds to the record rather than
    rewriting it, so history cannot be silently lost. Purely local -- creating
    or archiving a conversation never touches GitHub."""

    def __init__(self, repo_name: str, issue_number, conversation_id=None):
        self.repo = repo_name
        self.issue = int(issue_number)
        self.dir = conversation_dir(repo_name, issue_number)
        self.id = conversation_id or new_conversation_id()

    # paths
    @property
    def metadata_path(self) -> Path:
        return self.dir / "metadata.json"

    @property
    def transcript_path(self) -> Path:
        return self.dir / "transcript.log"

    @property
    def messages_path(self) -> Path:
        return self.dir / "messages.jsonl"

    @property
    def archive_dir(self) -> Path:
        return self.dir / "archive"

    # lifecycle
    @classmethod
    def load(cls, repo_name: str, issue_number):
        """Existing conversation, or None. Never creates -- this is what makes
        ``--resume`` able to fail loudly instead of silently starting over."""
        meta = read_json_tolerant(
            conversation_dir(repo_name, issue_number) / "metadata.json",
            default=None, label="conversation metadata",
        )
        if not isinstance(meta, dict) or not meta.get("conversation_id"):
            return None
        conv = cls(repo_name, issue_number, meta["conversation_id"])
        conv._meta = meta
        return conv

    @classmethod
    def open_or_create(cls, repo_name: str, issue_number, provider="", model=""):
        conv = cls.load(repo_name, issue_number)
        if conv is not None:
            conv.touch(provider=provider, model=model)
            return conv, False
        conv = cls(repo_name, issue_number)
        conv.create(provider=provider, model=model)
        return conv, True

    def create(self, provider="", model="") -> dict:
        self.dir.mkdir(parents=True, exist_ok=True)
        self._meta = {
            "conversation_id": self.id,
            "repo": self.repo,
            "issue": self.issue,
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "provider": provider,
            "model": model,
            "message_count": 0,
            "rounds": 0,
            "archived_conversation_ids": [],
            "transcript": rel_to_home(self.transcript_path),
            "messages": rel_to_home(self.messages_path),
        }
        atomic_write_json(self.metadata_path, self._meta)
        self.note(f"conversation {self.id} opened for {self.repo}#{self.issue}")
        return self._meta

    @property
    def meta(self) -> dict:
        if not hasattr(self, "_meta"):
            self._meta = read_json_tolerant(
                self.metadata_path, default={}, label="conversation metadata"
            ) or {}
        return self._meta

    def touch(self, provider="", model="", **fields) -> None:
        meta = self.meta
        meta["updated_at"] = now_iso()
        if provider:
            meta["provider"] = provider
        if model:
            meta["model"] = model
        meta.update(fields)
        atomic_write_json(self.metadata_path, meta)

    # writing
    def _heal_trailing_newline(self, path: Path) -> None:
        """A process killed mid-write leaves a partial last line. Without this,
        the next append fuses onto it and BOTH records become unreadable -- so
        one crash would cost two turns instead of none."""
        try:
            if not path.exists() or path.stat().st_size == 0:
                return
            with open(path, "rb") as fh:
                fh.seek(-1, os.SEEK_END)
                if fh.read(1) == b"\n":
                    return
            with open(path, "a", encoding="utf-8") as fh:
                fh.write("\n")
        except OSError:
            pass

    def note(self, text: str) -> None:
        """A line in the human transcript only -- stage changes, guards fired,
        commands run. Failures here are printed, never raised: losing a log line
        must not abort a workflow."""
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            self._heal_trailing_newline(self.transcript_path)
            with open(self.transcript_path, "a", encoding="utf-8") as fh:
                fh.write(f"[{now_iso()}] {text}\n")
        except OSError as exc:
            print(f"⚠️  Could not write transcript {self.transcript_path}: {exc}")

    def append(self, role: str, content: str, kind="message", meta=None) -> None:
        """One LLM turn (or tool result) in both logs."""
        self.dir.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": now_iso(),
            "conversation_id": self.id,
            "role": role,
            "kind": kind,
            "content": content if isinstance(content, str) else str(content),
            "meta": meta or {},
        }
        try:
            self._heal_trailing_newline(self.messages_path)
            with open(self.messages_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, default=str) + "\n")
        except OSError as exc:
            print(f"⚠️  Could not append to {self.messages_path}: {exc}")
            return
        preview = entry["content"].strip().replace("\n", " ")
        if len(preview) > 300:
            preview = preview[:300] + f"... (+{len(entry['content']) - 300} chars)"
        self.note(f"{role}/{kind}: {preview}")
        m = self.meta
        m["message_count"] = int(m.get("message_count", 0)) + 1
        self.touch()

    # reading
    def messages(self) -> list:
        """All turns. A truncated final line (killed mid-write) is skipped
        rather than fatal, so a crash during logging can't brick a resume."""
        if not self.messages_path.exists():
            return []
        out = []
        try:
            with open(self.messages_path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError as exc:
            print(f"⚠️  Could not read {self.messages_path}: {exc}")
        return out

    def tail(self, n=6) -> list:
        return self.messages()[-n:]

    def recent_context(self, max_chars=6000, n=8) -> str:
        """Prior turns as a text block to prepend to a prompt. Truncation is
        oldest-first and per-entry, and says so, so the model is never silently
        handed a half sentence."""
        chunks = []
        for entry in self.tail(n):
            body = str(entry.get("content", ""))
            if len(body) > 1200:
                body = body[:1200] + " ...[truncated]"
            chunks.append(f"[{entry.get('ts')}] {entry.get('role')}: {body}")
        text = "\n\n".join(chunks)
        if len(text) > max_chars:
            text = "...[earlier turns omitted]\n\n" + text[-max_chars:]
        return text

    def transcript_tail(self, lines=40) -> list:
        if not self.transcript_path.exists():
            return []
        try:
            return self.transcript_path.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()[-lines:]
        except OSError:
            return []

    # --new-conversation: version the old one, never delete it
    def archive_and_restart(self, reason="") -> "Conversation":
        """Move metadata/transcript/messages into ``archive/<old-id>/`` and open
        a fresh conversation for the same issue. Nothing is deleted, so a
        mistaken ``--new-conversation`` is recoverable by hand."""
        old_id = self.meta.get("conversation_id") or self.id
        dest = self.archive_dir / _safe_component(old_id)
        dest.mkdir(parents=True, exist_ok=True)
        moved = []
        for path in (self.metadata_path, self.transcript_path, self.messages_path):
            if not path.exists():
                continue
            target = dest / path.name
            if target.exists():                       # never clobber an archive
                target = dest / f"{path.stem}-{int(time.time())}{path.suffix}"
            try:
                shutil.move(str(path), str(target))
                moved.append(target.name)
            except OSError as exc:
                raise SessionStoreError(
                    f"Could not archive {path} -> {target}: {exc}\n"
                    f"   The old conversation was left in place; nothing was lost.\n"
                    f"   Close anything holding the file and retry."
                ) from exc
        fresh = Conversation(self.repo, self.issue)
        fresh.create(provider=self.meta.get("provider", ""),
                     model=self.meta.get("model", ""))
        archived = list(self.meta.get("archived_conversation_ids") or [])
        archived.append(old_id)
        fresh.touch(archived_conversation_ids=archived,
                    restarted_from=old_id,
                    restart_reason=reason or "user requested --new-conversation")
        fresh.note(f"archived conversation {old_id} ({', '.join(moved) or 'no files'}) "
                   f"to {rel_to_home(dest)}; reason: {reason or 'not given'}")
        return fresh

    def summary(self) -> dict:
        meta = dict(self.meta)
        meta.update({
            "conversation_dir": rel_to_home(self.dir),
            "message_count_on_disk": len(self.messages()),
            "archived": sorted(
                p.name for p in self.archive_dir.glob("*") if p.is_dir()
            ) if self.archive_dir.exists() else [],
        })
        return meta


# --- workspace isolation + destructive-action guard -----------------------
# The bug this prevents: the old code cloned into `workspace/<repo-name>` (no
# owner, no issue) and then ran `git reset --hard` + `git clean -fd`
# unconditionally. Two issues in the same repo -- or two repos with the same
# name under different owners -- shared one directory, so starting the second
# workflow silently destroyed the first one's uncommitted work. Now every
# workflow owns its own directory and stamps it, and every destructive git call
# goes through guard_before_destructive() first.
OWNER_MARKER = ".agent_owner.json"


def _run_git(args, cwd, timeout=30):
    """(returncode, stdout+stderr). Never raises; git being absent or the
    directory not being a repo is information, not a crash."""
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except (OSError, ValueError) as exc:
        return 127, f"git unavailable: {exc}"
    except subprocess.SubprocessError as exc:
        return 124, f"git failed: {exc}"


def owner_marker_path(workspace) -> Path:
    """Kept as a SIBLING of the clone (``issue-52.owner.json`` next to
    ``issue-52/``) rather than inside it. Two reasons: ``git clone`` refuses a
    non-empty destination, so a marker written before cloning would break the
    clone; and the claim then survives the clone being deleted, so a re-clone
    still knows whose directory this is."""
    path = Path(workspace)
    return path.parent / f"{path.name}.owner.json"


def write_owner_marker(workspace, repo_name, issue_number, conversation_id="",
                       branch="") -> None:
    atomic_write_json(owner_marker_path(workspace), {
        "repo": repo_name,
        "issue": int(issue_number),
        "conversation_id": conversation_id,
        "branch": branch,
        "pid": os.getpid(),
        "stamped_at": now_iso(),
    })


def read_owner_marker(workspace):
    return read_json_tolerant(
        owner_marker_path(workspace), default=None, label="workspace owner marker"
    )


def is_git_repo(workspace) -> bool:
    return (Path(workspace) / ".git").exists()


def workspace_is_dirty(workspace):
    """(dirty, [porcelain lines]). The owner marker is filtered defensively --
    it normally lives outside the clone, but a marker written by an earlier
    version (or copied in by hand) must never be mistaken for the user's work."""
    path = Path(workspace)
    if not is_git_repo(path):
        return False, []
    code, out = _run_git(["status", "--porcelain"], path)
    if code != 0:
        return False, []
    lines = [ln for ln in out.splitlines() if ln.strip()
             and OWNER_MARKER not in ln]
    return bool(lines), lines


def current_branch(workspace):
    path = Path(workspace)
    if not is_git_repo(path):
        return None
    code, out = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], path)
    branch = out.strip().splitlines()[-1].strip() if out.strip() else ""
    return branch if code == 0 and branch else None


def describe_workspace(workspace) -> dict:
    """Read-only snapshot for --status / --list-workflows. No git writes."""
    path = Path(workspace)
    info = {"path": str(path), "exists": path.exists(), "is_git": False,
            "branch": None, "dirty": False, "dirty_files": []}
    if not info["exists"]:
        return info
    info["is_git"] = is_git_repo(path)
    if info["is_git"]:
        info["branch"] = current_branch(path)
        info["dirty"], info["dirty_files"] = workspace_is_dirty(path)
    return info


def assert_workspace_owner(workspace, repo_name, issue_number) -> None:
    """Refuse to touch a directory another workflow stamped. An unstamped
    directory is allowed (it predates this feature, or the user made it) --
    the caller stamps it on the way in."""
    marker = read_owner_marker(workspace)
    if not marker:
        return
    same = (str(marker.get("repo")) == str(repo_name)
            and int(marker.get("issue", -1)) == int(issue_number))
    if same:
        return
    raise WorkspaceConflict(
        f"Workspace {workspace} belongs to {marker.get('repo')} "
        f"issue #{marker.get('issue')}, not {repo_name} issue #{issue_number}.\n"
        f"   Refusing to touch another workflow's clone.\n"
        f"   Inspect it:  python oss_agent_v2.py --status {marker.get('issue')} "
        f"--repo {marker.get('repo')}\n"
        f"   Or move that directory aside manually if it is stale."
    )


def guard_before_destructive(workspace, repo_name, issue_number, action,
                             force=False):
    """THE choke point in front of every reset/clean/checkout/delete.

    Two independent checks, in this order: does another workflow own this
    directory (never overridable -- that is somebody else's work), and does it
    contain uncommitted changes (overridable with `force`, because a resumed
    workflow legitimately discards its own failed attempt).

    Returns the porcelain lines it decided to allow past, so the caller can log
    what was discarded."""
    path = Path(workspace)
    assert_workspace_owner(path, repo_name, issue_number)
    dirty, lines = workspace_is_dirty(path)
    if dirty and not force:
        shown = "\n".join(f"       {ln}" for ln in lines[:12])
        more = f"\n       ... and {len(lines) - 12} more" if len(lines) > 12 else ""
        raise WorkspaceConflict(
            f"Refusing to {action} in {path}: {len(lines)} uncommitted change(s).\n"
            f"{shown}{more}\n"
            f"   Nothing was modified. Choose one:\n"
            f"     * keep the work:    cd {path} && git stash   (or commit it)\n"
            f"     * pause instead:    python oss_agent_v2.py --leave {issue_number} "
            f"--repo {repo_name}\n"
            f"     * discard it:       re-run with --force-workspace"
        )
    return lines


def legacy_workspace_dirs() -> list:
    """Old shared clones at ``workspace/<repo-name>/``. Detected by having a
    ``.git`` one level down, which the new ``<owner-repo>/issue-<N>/`` layout
    never does. Reported, never deleted -- they may hold real work."""
    root = workspace_root()
    if not root.exists():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and (p / ".git").exists())


def ensure_workspace(repo_name, issue_number, conversation_id="", branch="") -> Path:
    """Create (if needed) and claim this workflow's own directory. Raises
    WorkspaceConflict rather than stealing one another workflow stamped."""
    path = workspace_dir(repo_name, issue_number)
    assert_workspace_owner(path, repo_name, issue_number)
    path.mkdir(parents=True, exist_ok=True)
    write_owner_marker(path, repo_name, issue_number, conversation_id, branch)
    return path


# --- status vocabulary ----------------------------------------------------
# Two layers on purpose. `state` stays the 17-value WF machine the agent has
# always enforced (and which the transition guard protects); `status` is the
# short human label the user asked for. Deriving the second from the first means
# they can never disagree -- there is only one source of truth.
PAUSED = "PAUSED"

STATUS_BY_WF = {
    "TASK_RECEIVED": "new",
    "ANALYZING": "analyzing",
    "IMPLEMENTING": "implementing",
    "TESTING": "testing",
    "DRAFT_PR_CREATED": "pr_open",
    "WAITING_FOR_FEEDBACK": "waiting_for_review",
    "PROCESS_FEEDBACK": "changes_requested",
    "OPTIMIZING": "implementing",
    "UPDATE_PR": "pr_open",
    "READY_FOR_HUMAN_APPROVAL": "ready_to_commit",
    "HUMAN_APPROVED": "ready_to_commit",
    "FINAL_VALIDATION": "testing",
    "COMMIT": "ready_to_commit",
    "SIGN_OFF": "ready_to_commit",
    "COMPLETED": "completed",
    "ABANDONED": "abandoned",
    PAUSED: "paused",
}

# LEAVING and RESUMING are moments, not resting places: a workflow is only ever
# found paused, not "leaving". They are recorded in the transcript and in
# `last_transition_marker` so a crash mid-pause is still diagnosable, but they
# are deliberately NOT persisted WF states -- adding resting states nothing can
# leave is how state machines deadlock.
TRANSIENT_MARKERS = ("LEAVING", "RESUMING")


def derive_status(record: dict) -> str:
    """The user-facing label. Overrides come first because they carry more
    information than the raw state does."""
    state = str(record.get("state") or "TASK_RECEIVED")
    if state == PAUSED:
        return "paused"
    if record.get("failed_reason"):
        return "failed"
    if record.get("finished_locally"):
        return "finished"
    if state == "TESTING" and str(record.get("last_test_status")) == "failed":
        return "test_failed"
    return STATUS_BY_WF.get(state, state.lower())


def resume_instructions(record: dict) -> list:
    """The exact commands that continue this workflow. Persisted with the record
    so a resume never depends on remembering the syntax."""
    repo, issue = record.get("repo", "owner/repo"), record.get("issue", 0)
    base = f"python oss_agent_v2.py --repo {repo}"
    status = derive_status(record)
    if status in ("completed", "abandoned", "finished"):
        return [f"{base} --status {issue}    # terminal: nothing left to resume"]
    lines = [f"{base} --resume {issue}", f"{base} --conversation {issue}"]
    if record.get("pr_number"):
        lines.append(f"{base} --review-feedback {issue}   # pull maintainer comments")
        lines.append(f"{base} --issue {issue} -close      # stand down, keeps the branch")
    return lines


# --- pause / resume payloads ---------------------------------------------
# Pure data: these build the fields, the caller persists them. Keeping them
# side-effect-free is what lets the tests assert that `--leave` writes exactly
# this and touches nothing else.
def pause_payload(record: dict, reason="", conversation=None, workspace=None,
                  provider="", model="", trigger="--leave") -> dict:
    """Everything needed to walk away and come back. Explicitly NOT included:
    anything that would require a GitHub call to produce -- a pause must work
    offline, mid-outage, and after Ctrl+C."""
    repo, issue = record.get("repo"), record.get("issue")
    ws = Path(workspace) if workspace else workspace_dir(repo, issue)
    conv_id = getattr(conversation, "id", None) or record.get("conversation_id")
    conv_dir = getattr(conversation, "dir", None) or conversation_dir(repo, issue)
    state = record.get("state") or "TASK_RECEIVED"
    payload = {
        "state": PAUSED,
        "paused_from": state if state != PAUSED else record.get("paused_from"),
        "paused_at": now_iso(),
        "paused_reason": reason or f"paused by {trigger}",
        "paused_by": trigger,
        "last_stage": state if state != PAUSED else record.get("last_stage"),
        "last_transition_marker": "LEAVING",
        "conversation_id": conv_id,
        "conversation_dir": rel_to_home(conv_dir),
        "transcript": rel_to_home(Path(conv_dir) / "transcript.log"),
        "messages": rel_to_home(Path(conv_dir) / "messages.jsonl"),
        "workspace": rel_to_home(ws),
        "workspace_abs": str(ws),
        "branch": current_branch(ws) or record.get("branch"),
        "base_branch": record.get("base_branch"),
        "provider": provider or record.get("provider", ""),
        "model": model or record.get("model", ""),
        "pause_count": int(record.get("pause_count", 0)) + 1,
    }
    dirty, dirty_files = workspace_is_dirty(ws)
    payload["workspace_dirty"] = dirty
    payload["workspace_dirty_files"] = dirty_files[:50]
    merged = {**record, **payload}
    payload["status"] = derive_status(merged)
    payload["resume_instructions"] = resume_instructions(merged)
    return payload


def resume_payload(record: dict) -> dict:
    """Fields to write when coming back. The pause is moved into
    `pause_history` rather than erased, so `--conversation-info` can show that
    this workflow was interrupted three times -- useful evidence when a fix
    keeps stalling."""
    target = resume_target(record)
    history = list(record.get("pause_history") or [])
    if record.get("paused_at"):
        history.append({
            "paused_at": record.get("paused_at"),
            "paused_from": record.get("paused_from"),
            "reason": record.get("paused_reason"),
            "by": record.get("paused_by"),
            "resumed_at": now_iso(),
        })
    return {
        "state": target,
        "last_transition_marker": "RESUMING",
        "resumed_at": now_iso(),
        "resume_count": int(record.get("resume_count", 0)) + 1,
        "pause_history": history[-20:],
        "paused_at": None,
        "paused_reason": None,
        "paused_by": None,
    }


def resume_target(record: dict) -> str:
    """Where a paused workflow goes back to. Falls back to a *safe*, walkable
    state rather than guessing forward: if the pause point is unknown, a
    workflow that owns a PR belongs in the feedback loop, and one that doesn't
    belongs back at analysis. Never returns an approval-gated state -- a resume
    must not land past a human gate it never passed."""
    if record.get("state") != PAUSED:
        return record.get("state") or "TASK_RECEIVED"
    target = record.get("paused_from") or record.get("last_stage")
    gated = {"READY_FOR_HUMAN_APPROVAL", "HUMAN_APPROVED", "FINAL_VALIDATION",
             "COMMIT", "SIGN_OFF", "COMPLETED"}
    if not target or target in gated or target == PAUSED:
        return "WAITING_FOR_FEEDBACK" if record.get("pr_number") else "ANALYZING"
    return target
