"""KB inbox: a JSONL file of KB updates captured from chat, drained into the repos by the sweep.

An update is anything the docs should reflect: a decision or plan, an event ("replaced the RO
filter"), a purchase or install, a config change, a correction.

Standard library only, so the sweep image (Debian's python3, no kb-mcp deps) can run this same
file as its `kb-inbox` CLI. Every change takes an exclusive flock on a sidecar lock file and
rewrites the whole file atomically (temp file + rename), so kb-mcp and the sweep, in separate
containers sharing the `kb-inbox` volume, never clobber each other or leave a half-written file.

Each entry carries a `rev` that bumps on every change. The sweep passes the rev it worked from
when it marks an entry; if a chat update landed in between, the mark is refused and the entry
is simply picked up again on the next run.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows, local tests only
    fcntl = None

STATUSES = ("pending", "needs-review", "done", "discarded")
# What update() may set. Only the sweep sets done; discarded is the chat-side way to drop an
# entry: it keeps the record, never reaches the sweep, and needs no run.
CHAT_STATUSES = ("pending", "needs-review", "discarded")
CLOSED = ("done", "discarded")
SWEEP_STATUSES = ("done", "needs-review")

LIMITS = {"topic": 120, "content": 20000, "context": 8000, "resolution": 4000, "note": 2000,
          "log": 4000}
MAX_OPEN = 200          # pending + needs-review; a runaway chat can't flood the sweep
MAX_FILE_BYTES = 8_000_000   # 200 open entries at the field limits fit
CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class InboxError(ValueError):
    """A refused request; the message is safe to show to the caller."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean(value: str | None, field: str, *, required: bool = False, single_line: bool = False) -> str:
    text = CTRL_RE.sub("", (value or "").replace("\r\n", "\n")).strip()
    if single_line:
        text = " ".join(text.split())
    if required and not text:
        raise InboxError(f"{field} must not be empty")
    limit = LIMITS["note" if field == "notes" else field]
    if len(text) > limit:
        raise InboxError(f"{field} is {len(text)} characters; the limit is {limit}")
    return text


class Inbox:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.archive_path = self.path.with_name(self.path.stem + "-archive.jsonl")

    # ---------- storage ----------

    @contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "a+") as fh:
            if fcntl:
                fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl:
                    fcntl.flock(fh, fcntl.LOCK_UN)

    def _load(self) -> list[dict]:
        if not self.path.exists():
            return []
        entries = []
        for n, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip():
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError as e:
                    # Refuse to rewrite (and so silently drop) anything we can't parse.
                    raise InboxError(f"inbox line {n} is corrupt; fix {self.path} by hand") from e
        return entries

    def _write(self, entries: list[dict], path: Path | None = None) -> None:
        path = path or self.path
        data = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries)
        if path == self.path and len(data.encode()) > MAX_FILE_BYTES:
            raise InboxError("inbox is full; the sweep needs to drain or archive it first")
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".inbox-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    @staticmethod
    def _find(entries: list[dict], entry_id: str) -> dict:
        for e in entries:
            if e["id"] == entry_id:
                return e
        raise InboxError(f"no inbox entry with id {entry_id!r}")

    # ---------- chat side (exposed as MCP tools) ----------

    def append(self, topic: str, content: str, context: str = "") -> dict:
        entry = {
            "id": f"u-{datetime.now(timezone.utc):%Y%m%d}-{secrets.token_hex(3)}",
            "rev": 1,
            "timestamp": _now(),
            "updated": _now(),
            "topic": _clean(topic, "topic", required=True, single_line=True),
            "content": _clean(content, "content", required=True),
            "context": _clean(context, "context"),
            "status": "pending",
            "resolution": "",
            "notes": [],
            "log": [],
        }
        with self._locked():
            entries = self._load()
            if sum(e["status"] not in CLOSED for e in entries) >= MAX_OPEN:
                raise InboxError(f"{MAX_OPEN} entries are already open; resolve some first")
            entries.append(entry)
            self._write(entries)
        return entry

    def update(self, entry_id: str, resolution: str | None = None, note: str | None = None,
               status: str | None = None) -> dict:
        """Attach a resolution and/or a note, move between pending and needs-review, or discard.
        Can't delete, can't touch the content, can't mark done."""
        if status is not None and status not in CHAT_STATUSES:
            raise InboxError(f"status can only be set to {' or '.join(CHAT_STATUSES)}")
        resolution = _clean(resolution, "resolution") if resolution is not None else None
        note = _clean(note, "notes") if note is not None else None
        if not (resolution or note or status):
            raise InboxError("nothing to update: give a resolution, a note, or a status")
        with self._locked():
            entries = self._load()
            e = self._find(entries, entry_id)
            if e["status"] in CLOSED:
                raise InboxError(f"{entry_id} is already {e['status']}; capture a new update instead")
            if resolution:
                e["resolution"] = resolution
                status = status or "pending"  # a resolution re-queues it for the next sweep
            if note:
                e["notes"].append(f"{_now()} {note}")
            if status:
                e["status"] = status
            e["rev"] += 1
            e["updated"] = _now()
            self._write(entries)
        return e

    def list(self, status: str | None = None, limit: int = 50) -> list[dict]:
        if status is not None and status not in STATUSES:
            raise InboxError(f"status must be one of {', '.join(STATUSES)}")
        with self._locked():
            entries = self._load()
        hits = [e for e in entries if status is None or e["status"] == status]
        return list(reversed(hits))[:limit]  # newest first

    def get(self, entry_id: str) -> dict:
        with self._locked():
            return self._find(self._load(), entry_id)

    # ---------- sweep side (CLI only, never exposed over MCP) ----------

    def mark(self, entry_id: str, status: str, log: str, rev: int) -> dict:
        if status not in SWEEP_STATUSES:
            raise InboxError(f"status must be one of {', '.join(SWEEP_STATUSES)}")
        log = _clean(log, "log", required=True)
        with self._locked():
            entries = self._load()
            e = self._find(entries, entry_id)
            if e["status"] == "discarded":
                raise InboxError(f"{entry_id} was discarded; leaving it alone")
            if e["rev"] != rev:
                raise InboxError(f"{entry_id} changed since rev {rev} (now rev {e['rev']}); "
                                 "leave it for the next run")
            e["status"] = status
            e["log"].append(f"{_now()} {log}")
            e["rev"] += 1
            e["updated"] = _now()
            self._write(entries)
        return e

    def count(self, status: str) -> int:
        return len(self.list(status, limit=10**9))

    def archive(self, days: int = 30) -> int:
        """Move done and discarded entries untouched for `days` into the archive file."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        with self._locked():
            entries = self._load()
            old = [e for e in entries if e["status"] in CLOSED and e["updated"] < cutoff]
            if not old:
                return 0
            prior = self.archive_path.read_text(encoding="utf-8") if self.archive_path.exists() else ""
            kept = [json.loads(line) for line in prior.splitlines() if line.strip()]
            self._write(kept + old, self.archive_path)
            self._write([e for e in entries if e not in old])
        return len(old)


def default_path() -> Path:
    return Path(os.environ.get("INBOX_DIR", "/data/inbox")) / "updates.jsonl"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="kb-inbox", description=__doc__.split("\n")[0])
    p.add_argument("--path", type=Path, default=None, help="inbox file (default: $INBOX_DIR/updates.jsonl)")
    sub = p.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list", help="entries as JSON, newest first")
    ls.add_argument("--status", choices=STATUSES)
    ls.add_argument("--limit", type=int, default=50)
    sh = sub.add_parser("show", help="one entry as JSON")
    sh.add_argument("id")
    mk = sub.add_parser("mark", help="sweep result: done or needs-review, with a log line")
    mk.add_argument("id")
    mk.add_argument("--status", required=True, choices=SWEEP_STATUSES)
    mk.add_argument("--rev", required=True, type=int, help="the rev the sweep worked from")
    mk.add_argument("--log", required=True, help="commits made (repo + short sha), or why it is stuck")
    ct = sub.add_parser("count", help="number of entries with a status")
    ct.add_argument("--status", required=True, choices=STATUSES)
    ar = sub.add_parser("archive", help="move old done/discarded entries to the archive file")
    ar.add_argument("--days", type=int, default=30)
    args = p.parse_args(argv)

    inbox = Inbox(args.path or default_path())
    try:
        if args.cmd == "list":
            out = inbox.list(args.status, args.limit)
        elif args.cmd == "show":
            out = inbox.get(args.id)
        elif args.cmd == "mark":
            out = inbox.mark(args.id, args.status, args.log, args.rev)
        elif args.cmd == "count":
            print(inbox.count(args.status))
            return 0
        else:
            print(inbox.archive(args.days))
            return 0
    except InboxError as e:
        print(f"kb-inbox: {e}", file=sys.stderr)
        return 1
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
