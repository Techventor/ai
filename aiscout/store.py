"""SQLite snapshot store.

GitHub does not expose a repo's star history through the API, and the
stargazers endpoint stops paginating at 40 000 entries, so the biggest
repos cannot be reconstructed that way at all. The workable answer is to
snapshot on every run and diff against the past -- which means momentum
becomes available from the second run onward, and gets sharper as history
accumulates.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    candidates   INTEGER,
    enriched     INTEGER,
    degraded     INTEGER,
    notes        TEXT
);

CREATE TABLE IF NOT EXISTS snapshots (
    run_id       TEXT NOT NULL,
    repo_id      INTEGER NOT NULL,
    full_name    TEXT NOT NULL,
    captured_at  TEXT NOT NULL,
    stars        INTEGER,
    forks        INTEGER,
    open_issues  INTEGER,
    watchers     INTEGER,
    created_at   TEXT,
    pushed_at    TEXT,
    contributors INTEGER,
    commits_90d  INTEGER,
    releases     INTEGER,
    language     TEXT,
    license      TEXT,
    owner_type   TEXT,
    archived     INTEGER,
    topics       TEXT,
    description  TEXT,
    PRIMARY KEY (run_id, repo_id)
);

CREATE INDEX IF NOT EXISTS idx_snap_repo_time
    ON snapshots (repo_id, captured_at);
CREATE INDEX IF NOT EXISTS idx_snap_name
    ON snapshots (full_name);

CREATE TABLE IF NOT EXISTS scores (
    run_id        TEXT NOT NULL,
    repo_id       INTEGER NOT NULL,
    full_name     TEXT NOT NULL,
    influence     REAL,
    momentum      REAL,
    category      TEXT,
    flags         TEXT,
    weekly_stars  REAL,
    momentum_src  TEXT,
    PRIMARY KEY (run_id, repo_id)
);
"""


def _iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


@dataclass
class Store:
    path: str = "aiscout.db"

    def __post_init__(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---------------------------------------------------------------- runs

    def start_run(self) -> str:
        run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S") \
            + "-" + uuid.uuid4().hex[:6]
        self.conn.execute("INSERT INTO runs (run_id, started_at) VALUES (?, ?)",
                          (run_id, _iso(dt.datetime.now(dt.timezone.utc))))
        self.conn.commit()
        return run_id

    def finish_run(self, run_id: str, candidates: int, enriched: int,
                   degraded: bool, notes: str = "") -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at=?, candidates=?, enriched=?, "
            "degraded=?, notes=? WHERE run_id=?",
            (_iso(dt.datetime.now(dt.timezone.utc)), candidates, enriched,
             int(degraded), notes, run_id))
        self.conn.commit()

    def run_count(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM runs WHERE finished_at IS NOT NULL"
        ).fetchone()[0]

    # ----------------------------------------------------------- snapshots

    def save_snapshots(self, run_id: str, repos: list[dict]) -> None:
        now = _iso(dt.datetime.now(dt.timezone.utc))
        rows = []
        for r in repos:
            owner = r.get("owner") or {}
            lic = r.get("license") or {}
            rows.append((
                run_id, r["id"], r["full_name"], now,
                r.get("stargazers_count"), r.get("forks_count"),
                r.get("open_issues_count"), r.get("watchers_count"),
                r.get("created_at"), r.get("pushed_at"),
                r.get("contributors"), r.get("commits_90d"), r.get("releases"),
                r.get("language"), lic.get("spdx_id"), owner.get("type"),
                int(bool(r.get("archived"))),
                json.dumps(r.get("topics") or [], ensure_ascii=False),
                (r.get("description") or "")[:1000],
            ))
        self.conn.executemany(
            "INSERT OR REPLACE INTO snapshots (run_id, repo_id, full_name, "
            "captured_at, stars, forks, open_issues, watchers, created_at, "
            "pushed_at, contributors, commits_90d, releases, language, "
            "license, owner_type, archived, topics, description) "
            "VALUES (" + ",".join("?" * 19) + ")", rows)
        self.conn.commit()

    def save_scores(self, run_id: str, scored: list[dict]) -> None:
        rows = [(run_id, s["id"], s["full_name"], s["influence"],
                 s["momentum"], s["category"], ",".join(s["flags"]),
                 s["weekly_stars"], s["momentum_src"]) for s in scored]
        self.conn.executemany(
            "INSERT OR REPLACE INTO scores (run_id, repo_id, full_name, "
            "influence, momentum, category, flags, weekly_stars, momentum_src)"
            " VALUES (?,?,?,?,?,?,?,?,?)", rows)
        self.conn.commit()

    # ------------------------------------------------------------- history

    def baselines(self, target_days: int, tolerance: float = 0.6) -> dict[int, tuple[int, float]]:
        """For each repo, the best snapshot near `target_days` ago.

        Returns {repo_id: (stars_then, actual_days_elapsed)}. The candidate
        closest to the target wins, and anything outside
        [target*(1-tol), target*(1+tol)+14] is ignored so a 3-day-old
        snapshot is never passed off as a 30-day baseline.
        """
        lo = target_days * (1 - tolerance)
        hi = target_days * (1 + tolerance) + 14
        now = dt.datetime.now(dt.timezone.utc)
        rows = self.conn.execute(
            "SELECT repo_id, stars, captured_at FROM snapshots "
            "WHERE stars IS NOT NULL ORDER BY captured_at DESC").fetchall()

        best: dict[int, tuple[int, float]] = {}
        seen_err: dict[int, float] = {}
        for row in rows:
            try:
                then = dt.datetime.fromisoformat(row["captured_at"])
            except ValueError:
                continue
            if then.tzinfo is None:
                then = then.replace(tzinfo=dt.timezone.utc)
            days = (now - then).total_seconds() / 86400.0
            if days < lo or days > hi or days <= 0:
                continue
            err = abs(days - target_days)
            rid = row["repo_id"]
            if rid not in seen_err or err < seen_err[rid]:
                seen_err[rid] = err
                best[rid] = (row["stars"], days)
        return best

    def latest_run_id(self) -> str | None:
        row = self.conn.execute(
            "SELECT run_id FROM runs WHERE finished_at IS NOT NULL "
            "ORDER BY started_at DESC LIMIT 1").fetchone()
        return row["run_id"] if row else None

    def load_run(self, run_id: str) -> list[dict]:
        """Rebuild search-API-shaped repo records from a stored snapshot.

        Lets scoring be re-run offline against data already collected, so
        weights in config.py can be tuned without spending search quota.
        """
        rows = self.conn.execute(
            "SELECT * FROM snapshots WHERE run_id = ?", (run_id,)).fetchall()
        repos = []
        for r in rows:
            try:
                topics = json.loads(r["topics"] or "[]")
            except ValueError:
                topics = []
            repos.append({
                "id": r["repo_id"],
                "full_name": r["full_name"],
                "name": r["full_name"].split("/")[-1],
                "html_url": f"https://github.com/{r['full_name']}",
                "description": r["description"],
                "topics": topics,
                "language": r["language"],
                "stargazers_count": r["stars"],
                "forks_count": r["forks"],
                "open_issues_count": r["open_issues"],
                "watchers_count": r["watchers"],
                "created_at": r["created_at"],
                "pushed_at": r["pushed_at"],
                "archived": bool(r["archived"]),
                "license": {"spdx_id": r["license"]} if r["license"] else None,
                "owner": {"type": r["owner_type"]},
                "contributors": r["contributors"],
                "commits_90d": r["commits_90d"],
                "releases": r["releases"],
            })
        return repos

    def history(self, full_name: str, limit: int = 60) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT captured_at, stars, forks, contributors, commits_90d "
            "FROM snapshots WHERE full_name = ? COLLATE NOCASE "
            "ORDER BY captured_at DESC LIMIT ?", (full_name, limit)).fetchall()

    def close(self) -> None:
        self.conn.close()
