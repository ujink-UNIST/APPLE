from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core import APDLJob, APDLRunner, Err, ErrorKind, Ok, RetryPolicy

log = logging.getLogger(__name__)

DATA_DIR = Path("data")
RUNS_DIR = Path("runs")
DB_PATH = DATA_DIR / "apple_jobs.db"
TERMINAL_STATUSES = {"SUCCEEDED", "FAILED"}
ERROR_CODE_BY_KIND = {
    ErrorKind.LICENSE.name: "E101",
    ErrorKind.CONVERGENCE.name: "E201",
    ErrorKind.MEMORY.name: "E301",
    ErrorKind.IO.name: "E501",
    ErrorKind.SYNTAX.name: "E601",
    ErrorKind.TIMEOUT.name: "E901",
    ErrorKind.ASSERTION.name: "E901",
    ErrorKind.UNKNOWN.name: "E901",
}


@dataclass(frozen=True)
class JobRecord:
    job_id: str
    hash: str
    status: str
    script_path: str
    run_dir: str
    timeout: int
    error_code: str | None
    error_kind: str | None
    error_message: str | None
    results_path: str | None
    created_at: float
    started_at: float | None
    finished_at: float | None

    @property
    def elapsed_sec(self) -> float | None:
        if self.started_at is None:
            return None
        end = self.finished_at or time.time()
        return max(0.0, end - self.started_at)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                hash TEXT NOT NULL,
                status TEXT NOT NULL,
                script_path TEXT NOT NULL,
                run_dir TEXT NOT NULL,
                timeout INTEGER NOT NULL,
                error_code TEXT,
                error_kind TEXT,
                error_message TEXT,
                results_path TEXT,
                created_at REAL NOT NULL,
                started_at REAL,
                finished_at REAL
            )
            """
        )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
        if "error_code" not in columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN error_code TEXT")
        conn.execute("UPDATE jobs SET status = 'SUCCEEDED' WHERE status = 'MAPDL_OK'")
        conn.execute(
            """
            UPDATE jobs
               SET status = 'FAILED', error_code = COALESCE(error_code, 'E901')
             WHERE status LIKE 'MAPDL_%'
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs(status, created_at)")


def _row_to_job(row: sqlite3.Row) -> JobRecord:
    data = dict(row)
    data.setdefault("error_code", None)
    return JobRecord(**data)


def error_code_for_kind(error_kind: str | None) -> str:
    return ERROR_CODE_BY_KIND.get(error_kind or ErrorKind.UNKNOWN.name, "E901")


def create_job(job_id: str, macro_hash: str, script_path: Path, run_dir: Path, timeout: int) -> None:
    now = time.time()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO jobs (
                job_id, hash, status, script_path, run_dir, timeout,
                created_at
            ) VALUES (?, ?, 'QUEUED', ?, ?, ?, ?)
            """,
            (job_id, macro_hash, str(script_path.resolve()), str(run_dir.resolve()), timeout, now),
        )


def get_job(job_id: str) -> JobRecord | None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return _row_to_job(row) if row else None


def claim_next_job() -> JobRecord | None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM jobs WHERE status = 'QUEUED' ORDER BY created_at LIMIT 1"
        ).fetchone()
        if row is None:
            conn.commit()
            return None

        started_at = time.time()
        conn.execute(
            "UPDATE jobs SET status = 'RUNNING', started_at = ? WHERE job_id = ?",
            (started_at, row["job_id"]),
        )
        conn.commit()
        updated = get_job(row["job_id"])
        return updated


def finish_job(
    job_id: str,
    status: str,
    error_code: str | None = None,
    error_kind: str | None = None,
    error_message: str | None = None,
    results_path: Path | None = None,
) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            UPDATE jobs
               SET status = ?, error_code = ?, error_kind = ?, error_message = ?, results_path = ?, finished_at = ?
             WHERE job_id = ?
            """,
            (
                status,
                error_code,
                error_kind,
                error_message,
                str(results_path) if results_path else None,
                time.time(),
                job_id,
            ),
        )


def job_to_payload(job: JobRecord, include_results: bool = False) -> dict[str, Any]:
    results_csv = None
    if include_results and job.results_path:
        path = Path(job.results_path)
        if path.exists():
            results_csv = path.read_text(encoding="utf-8", errors="replace")

    return {
        "job_id": job.job_id,
        "status": job.status,
        "error_code": job.error_code,
        "error_kind": job.error_kind,
        "hash": job.hash,
        "schema_version": "1.0",
        "elapsed_sec": job.elapsed_sec,
        "results_csv": results_csv,
    }


class SQLiteJobWorker:
    """SQLite에 저장된 QUEUED 작업을 순차 실행하는 단일 worker."""

    def __init__(self, poll_interval: float = 1.0) -> None:
        self.poll_interval = poll_interval
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        init_db()
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run_loop, name="apple-sqlite-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                job = claim_next_job()
                if job is None:
                    time.sleep(self.poll_interval)
                    continue
                self._execute_job(job)
            except Exception:
                log.exception("SQLite job worker loop failed")
                time.sleep(self.poll_interval)

    def _execute_job(self, record: JobRecord) -> None:
        log.info("APPLE job started: %s", record.job_id)
        job = APDLJob(
            name=record.hash,
            script_path=Path(record.script_path),
            working_dir=Path(record.run_dir),
            timeout=record.timeout,
            extra_args={"override": True},
        )
        result = APDLRunner(retry_policy=RetryPolicy(max_attempts=1)).run(job)

        if isinstance(result, Ok):
            results_path = Path(record.run_dir) / "results.txt"
            if not results_path.exists():
                finish_job(
                    record.job_id,
                    status="FAILED",
                    error_code="E501",
                    error_kind="IO",
                    error_message="MAPDL 실행은 완료됐지만 results.txt가 생성되지 않았습니다.",
                )
                log.warning("APPLE job completed without results.txt: %s", record.job_id)
                return

            finish_job(
                record.job_id,
                status="SUCCEEDED",
                results_path=results_path,
            )
            log.info("APPLE job completed: %s", record.job_id)
            return

        if isinstance(result, Err):
            error = result.error
            finish_job(
                record.job_id,
                status="FAILED",
                error_code=error_code_for_kind(error.kind.name),
                error_kind=error.kind.name,
                error_message=str(error),
            )
            log.warning("APPLE job failed: %s %s", record.job_id, error)
            return

        finish_job(
            record.job_id,
            status="FAILED",
            error_code="E901",
            error_kind="UNKNOWN",
            error_message=json.dumps({"message": "Unknown runner result"}),
        )
