"""Job persistence: SQLite metadata (WAL) + files on disk.

Single-host design: the API and any number of worker *processes* share ``DATA_DIR``
(a local disk or a shared volume). Claiming a job is an atomic ``BEGIN IMMEDIATE``
transaction, so two workers can never take the same job.

Scaling out to several hosts means replacing this module with a Postgres + object
storage implementation of the same methods (see docs/ARCHITECTURE.md).
"""

from __future__ import annotations

import contextlib
import json
import re
import shutil
import sqlite3
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")

QUEUED, RUNNING, SUCCEEDED, FAILED, CANCELED = "queued", "running", "succeeded", "failed", "canceled"
FINISHED = (SUCCEEDED, FAILED, CANCELED)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    heartbeat_at REAL,
    worker TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    progress TEXT NOT NULL DEFAULT '',
    error TEXT,
    summary_json TEXT,
    spec_json TEXT NOT NULL,
    files_json TEXT NOT NULL,
    engine TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at);
"""


def new_job_id() -> str:
    return uuid.uuid4().hex


def valid_job_id(job_id: str) -> bool:
    return bool(JOB_ID_RE.match(job_id))


class JobStore:
    def __init__(self, data_dir: Path) -> None:
        self.root = Path(data_dir).resolve()
        self.jobs_dir = self.root / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "jobs.db"
        with self._conn() as c:
            c.executescript(_SCHEMA)

    # ------------------------------------------------------------------ infra
    @contextlib.contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA synchronous=NORMAL")
            yield conn
        finally:
            conn.close()

    def job_dir(self, job_id: str) -> Path:
        if not valid_job_id(job_id):
            raise ValueError("invalid job id")
        return self.jobs_dir / job_id

    def check(self) -> None:
        """Raise if the database is not usable (used by /readyz)."""
        with self._conn() as c:
            c.execute("SELECT 1").fetchone()

    # ------------------------------------------------------------------ create
    def write_inputs(self, job_id: str, files: dict[str, bytes]) -> None:
        d = self.job_dir(job_id) / "input"
        d.mkdir(parents=True, exist_ok=True)
        for name, data in files.items():
            (d / name).write_bytes(data)

    def read_inputs(self, job_id: str, names: list[str]) -> dict[str, bytes]:
        d = self.job_dir(job_id) / "input"
        return {n: (d / n).read_bytes() for n in names}

    def insert(self, job_id: str, *, name: str, spec_json: str, files: dict, engine: str) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO jobs (id, name, status, created_at, spec_json, files_json, engine) "
                "VALUES (?,?,?,?,?,?,?)",
                (job_id, name, QUEUED, time.time(), spec_json, json.dumps(files), engine),
            )

    def discard_files(self, job_id: str) -> None:
        shutil.rmtree(self.job_dir(job_id), ignore_errors=True)

    # -------------------------------------------------------------------- read
    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        d = dict(row)
        d["spec"] = json.loads(d.pop("spec_json"))
        d["files"] = json.loads(d.pop("files_json"))
        sj = d.pop("summary_json")
        d["summary"] = json.loads(sj) if sj else None
        d["cancel_requested"] = bool(d["cancel_requested"])
        return d

    def get(self, job_id: str) -> dict | None:
        if not valid_job_id(job_id):
            return None
        with self._conn() as c:
            return self._row(c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def list(self, limit: int = 50, offset: int = 0) -> list[dict]:
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ? OFFSET ?", (limit, offset)
            ).fetchall()
        out = [self._row(r) for r in rows]
        return [r for r in out if r is not None]

    def count_queued(self) -> int:
        with self._conn() as c:
            return int(c.execute("SELECT COUNT(*) FROM jobs WHERE status=?", (QUEUED,)).fetchone()[0])

    # ------------------------------------------------------------ worker side
    def claim_next(self, worker_id: str) -> dict | None:
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                row = c.execute(
                    "SELECT id FROM jobs WHERE status=? AND cancel_requested=0 ORDER BY created_at LIMIT 1",
                    (QUEUED,),
                ).fetchone()
                if row is None:
                    c.execute("COMMIT")
                    return None
                now = time.time()
                c.execute(
                    "UPDATE jobs SET status=?, worker=?, started_at=?, heartbeat_at=?, attempts=attempts+1, "
                    "progress='starting', error=NULL WHERE id=?",
                    (RUNNING, worker_id, now, now, row["id"]),
                )
                c.execute("COMMIT")
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    c.execute("ROLLBACK")
                raise
            return self._row(c.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())

    def heartbeat(self, job_id: str, worker_id: str, progress: str | None = None) -> None:
        with self._conn() as c:
            if progress is None:
                c.execute(
                    "UPDATE jobs SET heartbeat_at=? WHERE id=? AND worker=? AND status=?",
                    (time.time(), job_id, worker_id, RUNNING),
                )
            else:
                c.execute(
                    "UPDATE jobs SET heartbeat_at=?, progress=? WHERE id=? AND worker=? AND status=?",
                    (time.time(), progress[:200], job_id, worker_id, RUNNING),
                )

    def is_cancel_requested(self, job_id: str) -> bool:
        with self._conn() as c:
            row = c.execute("SELECT cancel_requested FROM jobs WHERE id=?", (job_id,)).fetchone()
        return bool(row and row["cancel_requested"])

    def finish(
        self, job_id: str, status: str, *, summary: dict | None = None, error: str | None = None
    ) -> None:
        if status not in FINISHED:
            raise ValueError(f"bad final status {status!r}")
        with self._conn() as c:
            c.execute(
                "UPDATE jobs SET status=?, finished_at=?, summary_json=?, error=?, progress='' "
                "WHERE id=? AND status=?",
                (status, time.time(), json.dumps(summary) if summary else None, error, job_id, RUNNING),
            )

    def requeue(self, job_id: str, worker_id: str) -> None:
        """Give a running job back to the queue (graceful worker shutdown)."""
        with self._conn() as c:
            c.execute(
                "UPDATE jobs SET status=?, worker=NULL, progress='', attempts=MAX(attempts-1,0) "
                "WHERE id=? AND worker=? AND status=?",
                (QUEUED, job_id, worker_id, RUNNING),
            )

    def recover_stale(self, stale_after_s: float, max_attempts: int) -> int:
        """Re-queue (or fail) running jobs whose worker stopped sending heartbeats."""
        cutoff = time.time() - stale_after_s
        n = 0
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                rows = c.execute(
                    "SELECT id, attempts FROM jobs WHERE status=? AND heartbeat_at < ?", (RUNNING, cutoff)
                ).fetchall()
                for r in rows:
                    if r["attempts"] >= max_attempts:
                        c.execute(
                            "UPDATE jobs SET status=?, finished_at=?, error=?, progress='' WHERE id=?",
                            (FAILED, time.time(), "worker stopped responding (gave up after retries)", r["id"]),
                        )
                    else:
                        c.execute(
                            "UPDATE jobs SET status=?, worker=NULL, progress='' WHERE id=?", (QUEUED, r["id"])
                        )
                    n += 1
                c.execute("COMMIT")
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    c.execute("ROLLBACK")
                raise
        return n

    # --------------------------------------------------------------- user side
    def cancel(self, job_id: str) -> str | None:
        """Cancel a job. Returns the resulting state or None if unknown/finished."""
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                row = c.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
                if row is None or row["status"] in FINISHED:
                    c.execute("COMMIT")
                    return None
                if row["status"] == QUEUED:
                    c.execute(
                        "UPDATE jobs SET status=?, finished_at=?, cancel_requested=1 WHERE id=?",
                        (CANCELED, time.time(), job_id),
                    )
                    state = CANCELED
                else:
                    c.execute("UPDATE jobs SET cancel_requested=1 WHERE id=?", (job_id,))
                    state = "canceling"
                c.execute("COMMIT")
                return state
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    c.execute("ROLLBACK")
                raise

    def delete(self, job_id: str) -> bool:
        """Delete a finished job and its files. Running jobs cannot be deleted."""
        with self._conn() as c:
            row = c.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or row["status"] in (QUEUED, RUNNING):
                return False
            c.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        self.discard_files(job_id)
        return True

    def cleanup_old(self, retention_days: int) -> int:
        if retention_days <= 0:
            return 0
        cutoff = time.time() - retention_days * 86400
        with self._conn() as c:
            rows = c.execute(
                "SELECT id FROM jobs WHERE status IN (?,?,?) AND COALESCE(finished_at, created_at) < ?",
                (*FINISHED, cutoff),
            ).fetchall()
        n = 0
        for r in rows:
            if self.delete(r["id"]):
                n += 1
        return n
