"""SQLite persistence: runs, approvals, usage, and a tamper-evident audit log.

Audit records are hash-chained: each row stores sha256(previous_hash + canonical JSON of the
event). Editing or deleting any row breaks every hash after it, which `verify()` detects.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    run_id     TEXT,
    actor      TEXT NOT NULL,
    event      TEXT NOT NULL,
    data       TEXT NOT NULL,
    prev_hash  TEXT NOT NULL,
    hash       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    role       TEXT NOT NULL,
    status     TEXT NOT NULL,
    state      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
    id          TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL,
    tool        TEXT NOT NULL,
    args        TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    status      TEXT NOT NULL,      -- pending | approved | denied
    decided_by  TEXT,
    note        TEXT,
    created_at  TEXT NOT NULL,
    decided_at  TEXT
);
CREATE TABLE IF NOT EXISTS usage (
    user_id TEXT NOT NULL,
    day     TEXT NOT NULL,
    tokens  INTEGER NOT NULL,
    PRIMARY KEY (user_id, day)
);
"""

GENESIS = "0" * 64


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _hash(prev: str, record: dict[str, Any]) -> str:
    body = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256((prev + body).encode()).hexdigest()


class Store:
    def __init__(self, path: str | Path = ":memory:") -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(SCHEMA)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ audit
    def audit(self, event: str, actor: str, data: dict[str, Any], run_id: str | None = None) -> str:
        with self._lock:
            row = self.con.execute("SELECT hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
            prev = row["hash"] if row else GENESIS
            record = {"ts": now(), "run_id": run_id, "actor": actor, "event": event, "data": data}
            h = _hash(prev, record)
            self.con.execute(
                "INSERT INTO audit (ts, run_id, actor, event, data, prev_hash, hash)"
                " VALUES (?,?,?,?,?,?,?)",
                (record["ts"], run_id, actor, event, json.dumps(data, default=str), prev, h),
            )
            return h

    def audit_log(self, run_id: str | None = None) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM audit", ()
        if run_id:
            sql, params = sql + " WHERE run_id = ?", (run_id,)  # type: ignore[assignment]
        rows = self.con.execute(sql + " ORDER BY seq", params).fetchall()
        return [{**dict(r), "data": json.loads(r["data"])} for r in rows]

    def verify(self) -> dict[str, Any]:
        """Recompute the chain. Returns {"ok": bool, "records": n, "broken_at": seq | None}."""
        prev, n = GENESIS, 0
        for r in self.con.execute("SELECT * FROM audit ORDER BY seq"):
            n += 1
            record = {
                "ts": r["ts"],
                "run_id": r["run_id"],
                "actor": r["actor"],
                "event": r["event"],
                "data": json.loads(r["data"]),
            }
            if r["prev_hash"] != prev or _hash(prev, record) != r["hash"]:
                return {"ok": False, "records": n, "broken_at": r["seq"]}
            prev = r["hash"]
        return {"ok": True, "records": n, "broken_at": None}

    # ------------------------------------------------------------------ runs
    def save_run(
        self, run_id: str, user_id: str, role: str, status: str, state: dict[str, Any]
    ) -> None:
        ts = now()
        self.con.execute(
            "INSERT INTO runs (id, user_id, role, status, state, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,"
            " state=excluded.state, updated_at=excluded.updated_at",
            (run_id, user_id, role, status, json.dumps(state, default=str), ts, ts),
        )

    def load_run(self, run_id: str) -> dict[str, Any]:
        r = self.con.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if r is None:
            raise KeyError(run_id)
        return {**dict(r), "state": json.loads(r["state"])}

    # ------------------------------------------------------------------ approvals
    def create_approval(
        self, run_id: str, tool: str, args: dict[str, Any], requested_by: str
    ) -> str:
        aid = uuid.uuid4().hex[:12]
        self.con.execute(
            "INSERT INTO approvals (id, run_id, tool, args, requested_by, status, created_at)"
            " VALUES (?,?,?,?,?, 'pending', ?)",
            (aid, run_id, tool, json.dumps(args, default=str), requested_by, now()),
        )
        return aid

    def decide_approval(self, aid: str, approved: bool, by: str, note: str | None) -> None:
        self.con.execute(
            "UPDATE approvals SET status=?, decided_by=?, note=?, decided_at=? WHERE id=?",
            ("approved" if approved else "denied", by, note, now(), aid),
        )

    def approvals(self, status: str | None = "pending", run_id: str | None = None) -> list[dict]:
        clauses, params = [], []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if run_id:
            clauses.append("run_id = ?")
            params.append(run_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.con.execute(f"SELECT * FROM approvals{where} ORDER BY created_at", params)
        return [{**dict(r), "args": json.loads(r["args"])} for r in rows]

    def approval(self, aid: str) -> dict[str, Any]:
        rows = [a for a in self.approvals(None) if a["id"] == aid]
        if not rows:
            raise KeyError(aid)
        return rows[0]

    # ------------------------------------------------------------------ usage
    def add_usage(self, user_id: str, tokens: int) -> int:
        day = date.today().isoformat()
        self.con.execute(
            "INSERT INTO usage (user_id, day, tokens) VALUES (?,?,?) ON CONFLICT(user_id, day)"
            " DO UPDATE SET tokens = tokens + excluded.tokens",
            (user_id, day, tokens),
        )
        return self.usage_today(user_id)

    def usage_today(self, user_id: str) -> int:
        r = self.con.execute(
            "SELECT tokens FROM usage WHERE user_id=? AND day=?",
            (user_id, date.today().isoformat()),
        ).fetchone()
        return int(r["tokens"]) if r else 0
