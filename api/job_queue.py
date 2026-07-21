from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.errors import ErrorClassifier, ErrorKind

log = logging.getLogger(__name__)

DATA_DIR = Path("data")
RUNS_DIR = Path("runs")
DB_PATH = DATA_DIR / "apple_jobs.db"
DEFAULT_ANSYS_EXE = r"C:\Program Files\ANSYS Inc\v232\ansys\bin\winx64\ANSYS232.exe"
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
_JOB_COLUMNS = (
    "job_id", "status", "run_dir", "timeout", "error_code", "error_kind",
    "error_message", "errors_json", "results_path", "created_at", "started_at", "finished_at",
)


@dataclass(frozen=True)
class JobRecord:
    job_id: str
    status: str
    run_dir: str
    timeout: int
    error_code: str | None
    error_kind: str | None
    error_message: str | None
    errors_json: str | None
    results_path: str | None
    created_at: float
    started_at: float | None
    finished_at: float | None

    @property
    def elapsed_sec(self) -> float | None:
        if self.started_at is None:
            return None
        return max(0.0, (self.finished_at or time.time()) - self.started_at)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


def _create_jobs_table(conn: sqlite3.Connection, name: str = "jobs") -> None:
    conn.execute(
        f"""
        CREATE TABLE {name} (
            job_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            run_dir TEXT NOT NULL,
            timeout INTEGER NOT NULL,
            error_code TEXT,
            error_kind TEXT,
            error_message TEXT,
            errors_json TEXT,
            results_path TEXT,
            created_at REAL NOT NULL,
            started_at REAL,
            finished_at REAL
        )
        """
    )


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(DB_PATH)) as conn, conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(jobs)")]
        if not columns:
            _create_jobs_table(conn)
        elif tuple(columns) != _JOB_COLUMNS:
            # 기존 macro/hash 스키마의 작업 이력은 보존하고 불필요한 열만 제거한다.
            conn.execute("DROP TABLE IF EXISTS jobs_new")
            _create_jobs_table(conn, "jobs_new")
            common = [column for column in _JOB_COLUMNS if column in columns]
            names = ", ".join(common)
            conn.execute(f"INSERT INTO jobs_new ({names}) SELECT {names} FROM jobs")
            conn.execute("DROP TABLE jobs")
            conn.execute("ALTER TABLE jobs_new RENAME TO jobs")

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
    return JobRecord(**dict(row))


def error_code_for_kind(error_kind: str | None) -> str:
    return ERROR_CODE_BY_KIND.get(error_kind or ErrorKind.UNKNOWN.name, "E901")


def classify_run_outputs(run_dir: Path) -> tuple[str | None, str | None]:
    """ANSYS 로그에서 알려진 실패 원인을 찾는다."""
    paths = sorted({*run_dir.glob("*.err"), *run_dir.glob("*.out"), run_dir / "runner.log"})
    for path in paths:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        kind, _code = ErrorClassifier.classify_detail(text)
        if kind != ErrorKind.UNKNOWN:
            return kind.name, f"{path.name}: {text[-2000:]}"
    return None, None


def create_job(job_id: str, run_dir: Path, timeout: int) -> None:
    with contextlib.closing(sqlite3.connect(DB_PATH)) as conn, conn:
        conn.execute(
            """
            INSERT INTO jobs (job_id, status, run_dir, timeout, created_at)
            VALUES (?, 'QUEUED', ?, ?, ?)
            """,
            (job_id, str(run_dir.resolve()), timeout, time.time()),
        )


def get_job(job_id: str) -> JobRecord | None:
    with contextlib.closing(sqlite3.connect(DB_PATH)) as conn, conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return _row_to_job(row) if row else None


def claim_next_job() -> JobRecord | None:
    with contextlib.closing(sqlite3.connect(DB_PATH)) as conn, conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM jobs WHERE status = 'QUEUED' ORDER BY created_at LIMIT 1"
        ).fetchone()
        if row is None:
            conn.commit()
            return None

        conn.execute(
            "UPDATE jobs SET status = 'RUNNING', started_at = ? WHERE job_id = ?",
            (time.time(), row["job_id"]),
        )
        conn.commit()
        return get_job(row["job_id"])


def finish_job(
    job_id: str,
    status: str,
    error_code: str | None = None,
    error_kind: str | None = None,
    error_message: str | None = None,
    error_kinds: list[str] | None = None,
    results_path: Path | None = None,
) -> None:
    with contextlib.closing(sqlite3.connect(DB_PATH)) as conn, conn:
        conn.execute(
            """
            UPDATE jobs
               SET status = ?, error_code = ?, error_kind = ?, error_message = ?, errors_json = ?, results_path = ?, finished_at = ?
             WHERE job_id = ?
            """,
            (
                status,
                error_code,
                error_kind,
                error_message,
                json.dumps([error_code_for_kind(kind) for kind in error_kinds]) if error_kinds else None,
                str(results_path) if results_path else None,
                time.time(),
                job_id,
            ),
        )


def tail_ansys_outputs(job_id: str, run_dir: Path, stop_event: threading.Event, poll_interval: float = 0.5) -> None:
    """ANSYS 로그 파일을 tail 해서 서버 로그로 출력한다."""
    positions: dict[Path, int] = {}

    while not stop_event.is_set():
        for pattern in ("*.out", "*.err", "*.log"):
            for path in sorted(run_dir.glob(pattern)):
                if not path.is_file():
                    continue
                previous_pos = positions.get(path, 0)
                try:
                    with path.open("r", encoding="utf-8", errors="replace") as fp:
                        fp.seek(previous_pos)
                        chunk = fp.read()
                        positions[path] = fp.tell()
                except OSError:
                    continue
                for line in chunk.splitlines():
                    log.info("ANSYS[%s][%s] %s", job_id, path.name, line)
        stop_event.wait(poll_interval)

    for pattern in ("*.out", "*.err", "*.log"):
        for path in sorted(run_dir.glob(pattern)):
            if not path.is_file():
                continue
            with contextlib.suppress(OSError):
                with path.open("r", encoding="utf-8", errors="replace") as fp:
                    fp.seek(positions.get(path, 0))
                    chunk = fp.read()
                for line in chunk.splitlines():
                    log.info("ANSYS[%s][%s] %s", job_id, path.name, line)


def ansys_command(jobname: str = "latsim_check") -> list[str]:
    executable = os.getenv("ANSYS_EXE", DEFAULT_ANSYS_EXE)
    try:
        processors = int(os.getenv("ANSYS_NP", "2"))
    except ValueError as exc:
        raise ValueError("ANSYS_NP는 양의 정수여야 합니다.") from exc
    if processors <= 0:
        raise ValueError("ANSYS_NP는 양의 정수여야 합니다.")
    if not jobname.replace("_", "").isalnum() or len(jobname) > 32:
        raise ValueError("ANSYS jobname must be an alphanumeric identifier")
    return [executable, "-b", "-np", str(processors), "-j", jobname, "-i", "setup.apdl", "-o", "solve.out"]


def execute_ansys(
    run_dir: Path,
    timeout: int,
    *,
    jobname: str = "latsim_check",
) -> tuple[Path | None, list[str], str | None]:
    """ANSYS를 실행하고 (결과 경로, 순서가 있는 오류 종류, 오류 메시지)를 반환한다."""
    try:
        command = ansys_command() if jobname == "latsim_check" else ansys_command(jobname)
        with (run_dir / "runner.log").open("wb") as runner_log:
            completed = subprocess.run(
                command,
                cwd=run_dir,
                timeout=timeout,
                stdout=runner_log,
                stderr=subprocess.STDOUT,
                check=False,
            )
    except subprocess.TimeoutExpired:
        return None, [ErrorKind.TIMEOUT.name, ErrorKind.IO.name], f"ANSYS 실행 제한 시간({timeout}초)을 초과했고 results.csv가 생성되지 않았습니다."
    except (OSError, ValueError) as exc:
        return None, [ErrorKind.IO.name], f"ANSYS 실행 실패: {exc}"

    results_path = run_dir / "results.csv"
    detected_kind, detail = classify_run_outputs(run_dir)
    error_kinds: list[str] = []
    messages: list[str] = []
    if completed.returncode != 0:
        error_kinds.append(detected_kind or ErrorKind.UNKNOWN.name)
        messages.append(detail or f"ANSYS가 종료 코드 {completed.returncode}로 실패했습니다.")
    if not results_path.is_file():
        if detected_kind and detected_kind not in error_kinds:
            error_kinds.append(detected_kind)
        if ErrorKind.IO.name not in error_kinds:
            error_kinds.append(ErrorKind.IO.name)
        messages.append("results.csv가 생성되지 않았습니다.")
    if error_kinds:
        return None, error_kinds, " ".join(messages)
    return results_path, [], None


def job_to_payload(job: JobRecord, include_results: bool = False) -> dict[str, Any]:
    results_csv = None
    if include_results and job.results_path:
        path = Path(job.results_path)
        if path.exists():
            results_csv = path.read_text(encoding="utf-8", errors="replace")

    errors = json.loads(job.errors_json) if job.errors_json else ([job.error_code] if job.error_code else [])
    return {
        "job_id": job.job_id,
        "status": job.status,
        "error_code": job.error_code,
        "error_kind": job.error_kind,
        "errors": errors,
        "schema_version": "2.0",
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
        self._stop_event.clear()
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
                    self._stop_event.wait(self.poll_interval)
                    continue
                self._execute_job(job)
            except Exception:
                log.exception("SQLite job worker loop failed")
                self._stop_event.wait(self.poll_interval)

    def _execute_job(self, record: JobRecord) -> None:
        log.info("APPLE job started: %s", record.job_id)
        run_dir = Path(record.run_dir)
        tail_stop_event = threading.Event()
        tail_thread = threading.Thread(
            target=tail_ansys_outputs,
            args=(record.job_id, run_dir, tail_stop_event),
            name=f"apple-ansys-tail-{record.job_id}",
            daemon=True,
        )
        tail_thread.start()
        try:
            results_path, error_kinds, error_message = execute_ansys(run_dir, record.timeout)
        finally:
            tail_stop_event.set()
            tail_thread.join(timeout=5)

        if results_path is not None:
            finish_job(record.job_id, status="SUCCEEDED", results_path=results_path)
            log.info("APPLE job completed: %s", record.job_id)
            return

        finish_job(
            record.job_id,
            status="FAILED",
            error_code=error_code_for_kind(error_kinds[0]),
            error_kind=error_kinds[0],
            error_message=error_message,
            error_kinds=error_kinds,
        )
        log.warning("APPLE job failed: %s %s", record.job_id, error_message)
