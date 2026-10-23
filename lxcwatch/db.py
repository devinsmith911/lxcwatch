import json
import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS containers (
    vmid INTEGER PRIMARY KEY,
    node TEXT NOT NULL,
    name TEXT,
    status TEXT,
    tags TEXT,
    info TEXT,
    error TEXT,
    scanned_at REAL,
    seen_at REAL,
    started_at REAL
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    vmid INTEGER NOT NULL,
    node TEXT NOT NULL,
    kind TEXT NOT NULL,
    snapshot TEXT,
    status TEXT NOT NULL,
    rc INTEGER,
    created_at REAL NOT NULL,
    finished_at REAL,
    log TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS jobs_vmid ON jobs (vmid, created_at);
CREATE TABLE IF NOT EXISTS notified (
    key TEXT PRIMARY KEY,
    at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


class DB:
    def __init__(self, data_dir: str):
        os.makedirs(data_dir, exist_ok=True)
        self.conn = sqlite3.connect(os.path.join(data_dir, "lxcwatch.db"), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)
            # Columns added after the first release.
            cols = {r[1] for r in self.conn.execute("PRAGMA table_info(containers)")}
            if "started_at" not in cols:
                self.conn.execute("ALTER TABLE containers ADD COLUMN started_at REAL")
                self.conn.commit()

    def _exec(self, sql: str, params=()) -> sqlite3.Cursor:
        with self.lock, self.conn:
            return self.conn.execute(sql, params)

    def _all(self, sql: str, params=()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    # containers

    def upsert_seen(self, vmid: int, node: str, name: str, status: str, tags: str, started_at: float | None = None) -> None:
        self._exec(
            """INSERT INTO containers (vmid, node, name, status, tags, seen_at, started_at) VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (vmid) DO UPDATE SET node=excluded.node, name=excluded.name,
               status=excluded.status, tags=excluded.tags, seen_at=excluded.seen_at, started_at=excluded.started_at""",
            (vmid, node, name, status, tags, time.time(), started_at),
        )

    def save_scan(self, vmid: int, info: dict | None, error: str | None) -> None:
        if info is None:
            self._exec("UPDATE containers SET error=?, scanned_at=? WHERE vmid=?", (error, time.time(), vmid))
        else:
            self._exec(
                "UPDATE containers SET info=?, error=?, scanned_at=? WHERE vmid=?",
                (json.dumps(info), error, time.time(), vmid),
            )

    def forget_missing(self, present: set[int]) -> None:
        rows = self._all("SELECT vmid FROM containers")
        for r in rows:
            if r["vmid"] not in present:
                self._exec("DELETE FROM containers WHERE vmid=?", (r["vmid"],))

    def containers(self) -> list[dict]:
        rows = self._all("SELECT * FROM containers ORDER BY vmid")
        for r in rows:
            r["info"] = json.loads(r["info"]) if r["info"] else None
        return rows

    def container(self, vmid: int) -> dict | None:
        rows = self._all("SELECT * FROM containers WHERE vmid=?", (vmid,))
        if not rows:
            return None
        r = rows[0]
        r["info"] = json.loads(r["info"]) if r["info"] else None
        return r

    # jobs

    def create_job(self, job_id: str, vmid: int, node: str, kind: str, snapshot: str | None) -> None:
        self._exec(
            "INSERT INTO jobs (id, vmid, node, kind, snapshot, status, created_at) VALUES (?, ?, ?, ?, ?, 'queued', ?)",
            (job_id, vmid, node, kind, snapshot, time.time()),
        )

    def update_job(self, job_id: str, **fields) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))

    def append_log(self, job_id: str, text: str) -> None:
        self._exec("UPDATE jobs SET log = log || ? WHERE id=?", (text, job_id))

    def job(self, job_id: str) -> dict | None:
        rows = self._all("SELECT * FROM jobs WHERE id=?", (job_id,))
        return rows[0] if rows else None

    def jobs(self, vmid: int | None = None, limit: int = 50) -> list[dict]:
        cols = "id, vmid, node, kind, snapshot, status, rc, created_at, finished_at"
        if vmid is None:
            return self._all(f"SELECT {cols} FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))
        return self._all(f"SELECT {cols} FROM jobs WHERE vmid=? ORDER BY created_at DESC LIMIT ?", (vmid, limit))

    def unfinished_jobs(self) -> list[dict]:
        return self._all("SELECT * FROM jobs WHERE status IN ('queued', 'running')")

    # notifications / misc

    def was_notified(self, key: str) -> bool:
        return bool(self._all("SELECT 1 FROM notified WHERE key=?", (key,)))

    def mark_notified(self, key: str) -> None:
        self._exec("INSERT OR IGNORE INTO notified (key, at) VALUES (?, ?)", (key, time.time()))

    def get(self, key: str) -> str | None:
        rows = self._all("SELECT value FROM kv WHERE key=?", (key,))
        return rows[0]["value"] if rows else None

    def set(self, key: str, value: str) -> None:
        self._exec("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value=excluded.value", (key, value))
