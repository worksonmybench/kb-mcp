"""Gitea sync + SQLite FTS5 index for the KB repos.

Repos are discovered by Gitea topic (default `kb`), mirrored under DATA_DIR/repos/<owner>/<name>,
and every text file is split into heading-sized sections and indexed. The index is rebuilt
from scratch whenever any repo's HEAD changes - the corpus is small, so this stays cheap and
avoids incremental-update bugs.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
from fastmcp.exceptions import ToolError

log = logging.getLogger("kb_mcp.store")

TEXT_EXTS = {
    ".md", ".markdown", ".txt", ".rst", ".yaml", ".yml", ".json", ".toml", ".ini", ".conf",
    ".cfg", ".csv", ".py", ".sh", ".ps1", ".js", ".ts", ".sql", ".xml", ".html", ".env.example",
}
TEXT_NAMES = {"README", "LICENSE", "Dockerfile", "Makefile", "CLAUDE.md"}
MAX_FILE_BYTES = 1_000_000
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")


@dataclass
class Config:
    gitea_url: str
    gitea_token: str
    topic: str = "kb"
    data_dir: Path = Path("/data")
    sync_interval: int = 300

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            gitea_url=os.environ["GITEA_URL"].rstrip("/"),
            gitea_token=os.environ.get("GITEA_TOKEN", ""),
            topic=os.environ.get("KB_TOPIC", "kb"),
            data_dir=Path(os.environ.get("DATA_DIR", "/data")),
            sync_interval=int(os.environ.get("SYNC_INTERVAL", "300")),
        )


class KBStore:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.repo_root = cfg.data_dir / "repos"
        self.db_path = cfg.data_dir / "index.sqlite"
        self.repo_root.mkdir(parents=True, exist_ok=True)
        self._sync_lock = threading.Lock()
        self.last_sync: float | None = None
        self.last_error: str | None = None
        self._heads: dict[str, str] = {}
        self._sigs: dict[str, str] = {}  # head + description: either changing triggers a reindex
        self._init_db()

    # ---------- db ----------

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=30)
        con.row_factory = sqlite3.Row
        return con

    def _init_db(self) -> None:
        with self._connect() as con:
            cols = {r["name"] for r in con.execute("PRAGMA table_info(repos)")}
            if cols and "overview" not in cols:
                # Older schema: drop it; the next sync rebuilds everything.
                con.execute("DROP TABLE repos")
                con.execute("DROP TABLE IF EXISTS sections")
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS repos (
                    full_name TEXT PRIMARY KEY, description TEXT, html_url TEXT,
                    head TEXT, last_commit_date TEXT, last_commit_msg TEXT, file_count INTEGER,
                    overview TEXT, layout TEXT
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS sections USING fts5(
                    repo UNINDEXED, path, heading, line UNINDEXED, body,
                    tokenize = 'porter unicode61 tokenchars ''._-'''
                );
                """
            )
            for row in con.execute("SELECT full_name, head, description FROM repos"):
                self._heads[row["full_name"]] = row["head"]
                self._sigs[row["full_name"]] = f'{row["head"]}:{row["description"]}'

    # ---------- gitea ----------

    def _auth_header(self) -> dict:
        # Optional: anonymous access covers public repos; a read-only token adds private ones.
        return {"Authorization": f"token {self.cfg.gitea_token}"} if self.cfg.gitea_token else {}

    def _api(self, path: str, **params) -> httpx.Response:
        r = httpx.get(
            f"{self.cfg.gitea_url}/api/v1{path}",
            params=params,
            headers=self._auth_header(),
            timeout=30,
        )
        r.raise_for_status()
        return r

    def discover(self) -> list[dict]:
        """All repos visible to the token that carry the KB topic (exact match)."""
        found, page = [], 1
        while True:
            data = self._api(
                "/repos/search", q=self.cfg.topic, topic="true", limit=50, page=page
            ).json()
            batch = data.get("data", [])
            for repo in batch:
                topics = repo.get("topics")
                if topics is None:
                    topics = self._api(f"/repos/{repo['full_name']}/topics").json().get("topics", [])
                if self.cfg.topic in topics and not repo.get("archived"):
                    found.append(repo)
            if len(batch) < 50:
                return found
            page += 1

    def _git(self, *args: str, cwd: Path | None = None) -> str:
        auth = ["-c", f"http.extraHeader=Authorization: token {self.cfg.gitea_token}"] if self.cfg.gitea_token else []
        # Keep git's post-fetch auto gc/maintenance in the foreground. Detached, it setsid()s
        # and is reparented to PID 1; Python never reaps it, and the zombies filled pids_limit.
        no_detach = ["-c", "gc.autoDetach=false", "-c", "maintenance.autoDetach=false"]
        return subprocess.run(
            ["git", *auth, *no_detach, *args],
            cwd=cwd, check=True, capture_output=True, text=True, timeout=300,
        ).stdout.strip()

    def _clone_url(self, repo: dict) -> str:
        # Use the configured base URL rather than repo["clone_url"], which reflects Gitea's
        # ROOT_URL and may not be reachable from this container.
        return f"{self.cfg.gitea_url}/{repo['full_name']}.git"

    def _mirror(self, repo: dict) -> Path:
        dest = self.repo_root / repo["full_name"]
        branch = repo.get("default_branch") or "main"
        if (dest / ".git").exists():
            self._git("fetch", "--quiet", "--depth", "200", "origin", branch, cwd=dest)
            self._git("reset", "--quiet", "--hard", "FETCH_HEAD", cwd=dest)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            self._git("clone", "--quiet", "--depth", "200", "--branch", branch,
                      self._clone_url(repo), str(dest))
        return dest

    # ---------- sync ----------

    def sync(self) -> dict:
        """Discover, mirror and (if anything changed) reindex. Safe to call concurrently."""
        if not self._sync_lock.acquire(blocking=False):
            return {"status": "already running"}
        try:
            repos = self.discover()
            wanted = {r["full_name"] for r in repos}
            changed, errors = False, {}
            meta = {}
            for repo in repos:
                name = repo["full_name"]
                if repo.get("empty"):
                    continue
                try:
                    path = self._mirror(repo)
                    head = self._git("rev-parse", "HEAD", cwd=path)
                    date, msg = self._git("log", "-1", "--format=%cI%x00%s", cwd=path).split("\x00", 1)
                    meta[name] = (repo, path, head, date, msg)
                    if self._sigs.get(name) != f"{head}:{repo.get('description') or ''}":
                        changed = True
                except Exception as e:  # keep the rest of the KB available
                    errors[name] = str(e)
                    log.warning("sync of %s failed: %s", name, e)
            removed = set(self._heads) - wanted
            for name in removed:
                shutil.rmtree(self.repo_root / name, ignore_errors=True)
                changed = True
            if changed:
                self._reindex(meta)
            self.last_sync = time.time()
            self.last_error = json.dumps(errors) if errors else None
            return {"status": "ok", "repos": sorted(meta), "reindexed": changed,
                    "removed": sorted(removed), "errors": errors}
        finally:
            self._sync_lock.release()

    def _reindex(self, meta: dict) -> None:
        con = self._connect()
        try:
            with con:
                con.execute("DELETE FROM repos")
                con.execute("DELETE FROM sections")
                for name, (repo, path, head, date, msg) in meta.items():
                    files = list(self._text_files(path))
                    for rel in files:
                        try:
                            text = (path / rel).read_text(encoding="utf-8")
                        except (UnicodeDecodeError, OSError):
                            continue
                        con.executemany(
                            "INSERT INTO sections(repo, path, heading, line, body) VALUES (?,?,?,?,?)",
                            [(name, rel, h, ln, body) for h, ln, body in split_sections(text)],
                        )
                    con.execute(
                        "INSERT INTO repos VALUES (?,?,?,?,?,?,?,?,?)",
                        (name, repo.get("description") or "", repo.get("html_url") or "",
                         head, date, msg, len(files), readme_overview(path),
                         json.dumps(layout(files))),
                    )
            self._heads = {n: m[2] for n, m in meta.items()}
            self._sigs = {n: f"{m[2]}:{m[0].get('description') or ''}" for n, m in meta.items()}
            log.info("reindexed %d repos", len(meta))
        finally:
            con.close()

    @staticmethod
    def _text_files(root: Path):
        for p in sorted(root.rglob("*")):
            if ".git" in p.parts or not p.is_file():
                continue
            if p.suffix.lower() not in TEXT_EXTS and p.name not in TEXT_NAMES:
                continue
            if p.stat().st_size > MAX_FILE_BYTES:
                continue
            yield p.relative_to(root).as_posix()

    def run_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.sync()
            except Exception as e:
                self.last_error = str(e)
                log.exception("sync failed")
            stop.wait(self.cfg.sync_interval)

    # ---------- queries ----------

    def resolve_repo(self, repo: str) -> str:
        """Accept 'owner/name' or a bare name (case-insensitive) if unambiguous."""
        names = list(self._heads)
        if repo in names:
            return repo
        hits = [n for n in names if n.lower() == repo.lower() or n.split("/", 1)[1].lower() == repo.lower()]
        if len(hits) == 1:
            return hits[0]
        raise ToolError(f"Unknown or ambiguous repo {repo!r}. Known: {', '.join(sorted(names))}")

    def safe_path(self, repo: str, path: str) -> Path:
        base = (self.repo_root / repo).resolve()
        target = (base / path.lstrip("/")).resolve()
        if base not in target.parents and target != base:
            raise ToolError("path escapes repository")
        if ".git" in target.relative_to(base).parts:
            raise ToolError("path not allowed")
        return target

    def sources(self) -> list[dict]:
        with self._connect() as con:
            rows = [dict(r) for r in con.execute("SELECT * FROM repos ORDER BY full_name")]
        for r in rows:
            r["layout"] = json.loads(r["layout"] or "{}")
            del r["head"]
        return rows

    def search(self, query: str, repo: str | None, limit: int) -> list[dict]:
        sql = (
            "SELECT repo, path, heading, line, "
            "snippet(sections, 4, '**', '**', ' ... ', 24) AS snippet, bm25(sections, 0, 4, 6, 0, 1) AS score "
            "FROM sections WHERE sections MATCH ? {} ORDER BY score LIMIT ?"
        )
        params: list = []
        extra = ""
        if repo:
            extra = "AND repo = ?"
            params.append(self.resolve_repo(repo))
        with self._connect() as con:
            for q in (query, fts_quote(query)):
                try:
                    rows = con.execute(sql.format(extra), [q, *params, limit]).fetchall()
                    return [dict(r) for r in rows]
                except sqlite3.OperationalError:
                    continue  # raw FTS syntax was invalid; retry as plain terms
        return []


def split_sections(text: str) -> list[tuple[str, int, str]]:
    """Split on markdown headings (outside code fences). Non-markdown files become one section."""
    lines = text.splitlines()
    out, heading, start, buf, fence = [], "", 1, [], False
    trail: list[str] = []
    for i, line in enumerate(lines, 1):
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
        m = None if fence else HEADING_RE.match(line)
        if m:
            if any(s.strip() for s in buf):
                out.append((heading, start, "\n".join(buf)))
            level = len(m.group(1))
            trail = trail[: level - 1] + [m.group(2)]
            heading, start, buf = " > ".join(trail), i, [line]
        else:
            buf.append(line)
    if any(s.strip() for s in buf):
        out.append((heading, start, "\n".join(buf)))
    return out


def fts_quote(query: str) -> str:
    terms = re.findall(r"[\w.\-/:]+", query)
    return " ".join('"' + t.replace('"', "") + '"' for t in terms) or '""'


README_NAMES = ("README.md", "readme.md", "Readme.md", "README.markdown", "README.txt", "README")
SKIP_LINE_RE = re.compile(r"^\s*(\**)?(last updated|previously updated|before that|earlier \d{4})", re.I)


def readme_overview(root: Path, limit: int = 900) -> str:
    """The README's opening prose: everything after the title up to the first subheading,
    minus 'Last updated' changelog lines and badges. Empty if there's no README."""
    for name in README_NAMES:
        if (root / name).is_file():
            text = (root / name).read_text(encoding="utf-8", errors="replace")
            break
    else:
        return ""
    out: list[str] = []
    for i, line in enumerate(text.splitlines()):
        if HEADING_RE.match(line):
            if i == 0 or not out:
                continue  # the title (or headings before any prose)
            break
        if SKIP_LINE_RE.match(line) or line.lstrip().startswith(("[![", "![")):
            continue
        out.append(line)
    overview = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
    return overview if len(overview) <= limit else overview[:limit].rsplit(" ", 1)[0] + " ..."


def layout(files: list[str]) -> dict:
    """Top-level text files, plus a file count per top-level directory."""
    top = [f for f in files if "/" not in f]
    dirs: dict[str, int] = {}
    for f in files:
        if "/" in f:
            d = f.split("/", 1)[0] + "/"
            dirs[d] = dirs.get(d, 0) + 1
    return {"files": top, "dirs": dirs}
