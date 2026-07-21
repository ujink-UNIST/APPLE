from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import math
import os
import re
import secrets
import shutil
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from api.job_queue import RUNS_DIR, ansys_command, execute_ansys
from api.routes.apple import MAX_ARCHIVE_BYTES, _extract_zip

POLL_SECONDS = 5
HEARTBEAT_SECONDS = 30
SIGNATURE_VERSION = "latsim-worker-v1"
_JOB_ID_PATTERN = re.compile(r"^analysis-[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class AgentConfig:
    api_url: str
    worker_id: str
    key_id: str
    private_key_path: Path
    solver_version: str
    ansys_np: int

    @classmethod
    def from_env(cls) -> "AgentConfig":
        api_url = os.environ.get("LATSIM_API_URL", "").rstrip("/")
        worker_id = os.environ.get("LATSIM_WORKER_ID", "")
        key_id = os.environ.get("LATSIM_WORKER_KEY_ID", "")
        private_key_path = Path(os.environ.get("LATSIM_WORKER_PRIVATE_KEY_PATH", ""))
        if not api_url.startswith("https://") and not (
            api_url.startswith("http://127.0.0.1") or api_url.startswith("http://localhost")
        ):
            raise ValueError("LATSIM_API_URL must use HTTPS outside localhost")
        if (
            re.fullmatch(r"[A-Za-z0-9_.@-]{1,64}", worker_id) is None
            or re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", key_id) is None
            or not private_key_path.is_file()
        ):
            raise ValueError(
                "LATSIM_WORKER_ID, LATSIM_WORKER_KEY_ID and a valid "
                "LATSIM_WORKER_PRIVATE_KEY_PATH are required"
            )
        _load_ed25519_private_key(private_key_path)
        command = ansys_command()
        executable = Path(command[0])
        if not executable.is_file():
            raise FileNotFoundError(f"ANSYS executable not found: {executable}")
        return cls(
            api_url=api_url,
            worker_id=worker_id,
            key_id=key_id,
            private_key_path=private_key_path,
            solver_version=os.environ.get("ANSYS_VERSION", executable.stem),
            ansys_np=int(os.environ.get("ANSYS_NP", "2")),
        )


class LatSimClient:
    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        self.private_key = _load_ed25519_private_key(config.private_key_path)

    def register(self) -> None:
        self.request(
            "POST",
            "/worker/register",
            json_body={
                "solver": "ansys_mapdl",
                "solver_version": self.config.solver_version,
                "analysis_types": ["periodic_static", "periodic_modal"],
                "ansys_np": self.config.ansys_np,
            },
        )

    def worker_heartbeat(self) -> None:
        self.request("PUT", "/worker/heartbeat")

    def claim(self) -> dict[str, Any] | None:
        status, payload, _headers = self.request("POST", "/worker/analysis/claim")
        if status == 204:
            return None
        claim = json.loads(payload)
        validate_claim(claim)
        return claim

    def download_setup(self, claim: dict[str, Any], destination: Path) -> None:
        status, payload, headers = self.request(
            "GET",
            f"/worker/analysis/{claim['analysis_job_id']}/setup",
            headers=self.lease_headers(claim),
            max_response_bytes=MAX_ARCHIVE_BYTES,
        )
        if status != 200:
            raise ValueError("Setup bundle response is invalid or too large")
        expected = str(claim["setup_bundle_sha256"])
        response_hash = headers.get("X-Setup-SHA256")
        if response_hash != expected or hashlib.sha256(payload).hexdigest() != expected:
            raise ValueError("Setup bundle SHA-256 mismatch")
        destination.write_bytes(payload)

    def heartbeat(self, claim: dict[str, Any]) -> None:
        self.request(
            "PUT",
            f"/worker/analysis/{claim['analysis_job_id']}/heartbeat",
            json_body={"attempt": claim["attempt"], "lease_token": claim["lease_token"]},
        )

    def upload_result(self, claim: dict[str, Any], results_path: Path) -> None:
        payload = results_path.read_bytes()
        self.request(
            "PUT",
            f"/worker/analysis/{claim['analysis_job_id']}/result",
            raw_body=payload,
            headers={
                **self.lease_headers(claim),
                "Content-Type": "text/csv",
                "X-Result-SHA256": hashlib.sha256(payload).hexdigest(),
            },
        )

    def report_failure(
        self,
        claim: dict[str, Any],
        error_code: str,
        error_kind: str,
        error_message: str,
    ) -> None:
        self.request(
            "POST",
            f"/worker/analysis/{claim['analysis_job_id']}/failure",
            json_body={
                "attempt": claim["attempt"],
                "lease_token": claim["lease_token"],
                "error_code": error_code,
                "error_kind": error_kind,
                "error_message": error_message[-2000:],
            },
        )

    def lease_headers(self, claim: dict[str, Any]) -> dict[str, str]:
        return {
            "X-Analysis-Attempt": str(claim["attempt"]),
            "X-Lease-Token": str(claim["lease_token"]),
        }

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        raw_body: bytes | None = None,
        headers: dict[str, str] | None = None,
        max_response_bytes: int | None = None,
    ) -> tuple[int, bytes, Any]:
        body = raw_body or b""
        base_headers = dict(headers or {})
        if json_body is not None:
            body = json.dumps(json_body, separators=(",", ":")).encode("utf-8")
            base_headers["Content-Type"] = "application/json"
        last_error: Exception | None = None
        for attempt in range(3):
            request_headers = {
                **base_headers,
                **worker_signature_headers(
                    self.config.worker_id,
                    self.config.key_id,
                    self.private_key,
                    method,
                    path,
                    body,
                ),
            }
            request = urllib.request.Request(
                f"{self.config.api_url}{path}",
                data=body or None,
                headers=request_headers,
                method=method,
            )
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    limit = None if max_response_bytes is None else max_response_bytes + 1
                    payload = response.read(limit)
                    if max_response_bytes is not None and len(payload) > max_response_bytes:
                        raise ValueError("LatSim response exceeds the configured size limit")
                    return response.status, payload, response.headers
            except urllib.error.HTTPError as exc:
                if 400 <= exc.code < 500:
                    detail = exc.read().decode("utf-8", errors="replace")
                    raise RuntimeError(
                        f"LatSim rejected {method} {path}: {exc.code} {detail}"
                    ) from exc
                last_error = exc
            except urllib.error.URLError as exc:
                last_error = exc
            time.sleep(2**attempt)
        raise RuntimeError(f"LatSim request failed: {method} {path}") from last_error


def worker_signature_headers(
    worker_id: str,
    key_id: str,
    private_key: Ed25519PrivateKey,
    method: str,
    path_and_query: str,
    body: bytes,
) -> dict[str, str]:
    timestamp = int(time.time())
    nonce = secrets.token_urlsafe(24)
    body_sha256 = hashlib.sha256(body).hexdigest()
    message = worker_signature_message(
        method,
        path_and_query,
        timestamp,
        nonce,
        body_sha256,
    )
    signature = base64.urlsafe_b64encode(private_key.sign(message)).rstrip(b"=").decode("ascii")
    return {
        "X-Worker-ID": worker_id,
        "X-Key-ID": key_id,
        "X-Timestamp": str(timestamp),
        "X-Nonce": nonce,
        "X-Content-SHA256": body_sha256,
        "X-Signature": signature,
    }


def worker_signature_message(
    method: str,
    path_and_query: str,
    timestamp: int,
    nonce: str,
    body_sha256: str,
) -> bytes:
    return "\n".join(
        (
            SIGNATURE_VERSION,
            method.upper(),
            path_and_query,
            str(timestamp),
            nonce,
            body_sha256,
        )
    ).encode("utf-8")


def _load_ed25519_private_key(path: Path) -> Ed25519PrivateKey:
    key = load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("Worker private key must be Ed25519")
    return key


def validate_claim(claim: Any) -> None:
    required = {
        "schema_version",
        "analysis_job_id",
        "setup_job_id",
        "setup_bundle_sha256",
        "analysis_type",
        "timeout_seconds",
        "attempt",
        "lease_token",
        "lease_expires_at",
    }
    if not isinstance(claim, dict) or set(claim) != required:
        raise ValueError("Claim payload fields do not match schema version 1")
    if claim["schema_version"] != 1:
        raise ValueError("Unsupported claim schema version")
    if not _JOB_ID_PATTERN.fullmatch(str(claim["analysis_job_id"])):
        raise ValueError("Invalid analysis_job_id in claim")
    if not re.fullmatch(r"setup-[A-Za-z0-9_-]+", str(claim["setup_job_id"])):
        raise ValueError("Invalid setup_job_id in claim")
    if not re.fullmatch(r"[0-9a-f]{64}", str(claim["setup_bundle_sha256"])):
        raise ValueError("Invalid setup bundle SHA-256 in claim")
    if claim["analysis_type"] not in {"periodic_static", "periodic_modal"}:
        raise ValueError("Unsupported analysis_type in claim")
    timeout_seconds = claim["timeout_seconds"]
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or timeout_seconds <= 0
    ):
        raise ValueError("Invalid timeout_seconds in claim")
    attempt = claim["attempt"]
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt <= 0:
        raise ValueError("Invalid attempt in claim")
    if not isinstance(claim["lease_token"], str) or len(claim["lease_token"]) < 32:
        raise ValueError("Invalid lease token in claim")
    lease_expires_at = claim["lease_expires_at"]
    if not isinstance(lease_expires_at, int | float) or lease_expires_at <= time.time():
        raise ValueError("Claim lease is already expired")


def run_claim(client: LatSimClient, claim: dict[str, Any]) -> None:
    analysis_job_id = str(claim.get("analysis_job_id", ""))
    if not _JOB_ID_PATTERN.fullmatch(analysis_job_id):
        raise ValueError("Invalid analysis_job_id in claim")
    run_dir = RUNS_DIR / analysis_job_id
    run_dir.mkdir(parents=True, exist_ok=True)
    setup_zip = run_dir / "setup.zip"
    marker_path = run_dir / "source.json"
    source = {
        "setup_job_id": claim["setup_job_id"],
        "setup_bundle_sha256": claim["setup_bundle_sha256"],
        "attempt": claim["attempt"],
    }

    results_path = run_dir / "results.csv"
    if results_path.is_file() and marker_path.is_file():
        if json.loads(marker_path.read_text(encoding="utf-8")) == source:
            validate_results(results_path, str(claim["analysis_type"]))
            client.upload_result(claim, results_path)
            return

    shutil.rmtree(run_dir, ignore_errors=True)
    run_dir.mkdir(parents=True)
    client.download_setup(claim, setup_zip)
    with setup_zip.open("rb") as source_file:
        _extract_zip(source_file, run_dir)
    setup_zip.unlink(missing_ok=True)
    for filename in ("setup.apdl", "mesh.cdb", "periodic_pairs.dat"):
        if not (run_dir / filename).is_file():
            raise ValueError(f"Setup bundle is missing {filename}")
    marker_path.write_text(json.dumps(source, sort_keys=True), encoding="utf-8")

    stop_event = threading.Event()
    lease_lost = threading.Event()

    def heartbeat_loop() -> None:
        while not stop_event.wait(HEARTBEAT_SECONDS):
            try:
                client.heartbeat(claim)
            except RuntimeError:
                lease_lost.set()
                return

    heartbeat = threading.Thread(target=heartbeat_loop, daemon=True)
    heartbeat.start()
    jobname = f"a{hashlib.sha256(analysis_job_id.encode()).hexdigest()[:15]}"
    # ponytail: the existing blocking runner cannot cancel mid-solve; lease fencing still rejects
    # stale results. Add Popen cancellation only if wasted solver time becomes measurable.
    result, error_kinds, error_message = execute_ansys(
        run_dir,
        timeout=int(claim["timeout_seconds"]),
        jobname=jobname,
    )
    stop_event.set()
    heartbeat.join(timeout=2)
    if lease_lost.is_set():
        raise RuntimeError("Analysis lease was lost while MAPDL was running")
    if result is None:
        kind = error_kinds[0] if error_kinds else "UNKNOWN"
        code = {
            "LICENSE": "E101",
            "CONVERGENCE": "E201",
            "MEMORY": "E301",
            "IO": "E501",
            "SYNTAX": "E601",
            "TIMEOUT": "E901",
        }.get(kind, "E901")
        client.report_failure(claim, code, kind, error_message or "ANSYS analysis failed")
        return
    validate_results(result, str(claim["analysis_type"]))
    client.upload_result(claim, result)


def validate_results(path: Path, analysis_type: str) -> None:
    rows = list(csv.reader(io.StringIO(path.read_text(encoding="utf-8-sig"))))
    expected = (
        ["mode", "freq"]
        if analysis_type == "periodic_modal"
        else ["step", "sub", "time", "out", "comp", "set", "val"]
    )
    if not rows or rows[0] != expected or len(rows) < 2:
        raise ValueError(f"results.csv header must be {','.join(expected)}")
    identifier = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
    for row in rows[1:]:
        if len(row) != len(expected):
            raise ValueError("results.csv contains a malformed row")
        if analysis_type == "periodic_modal":
            mode = int(row[0])
            values = [float(row[1])]
            if mode <= 0 or str(mode) != row[0].strip():
                raise ValueError("results.csv contains an invalid mode")
        else:
            substep = int(row[1])
            values = [float(row[2]), float(row[6])]
            if substep <= 0 or any(
                identifier.fullmatch(value) is None
                for value in (row[0], row[3], row[4], row[5])
            ):
                raise ValueError("results.csv contains an invalid static row")
        if not all(math.isfinite(value) for value in values):
            raise ValueError("results.csv contains a non-finite value")


def main() -> None:
    config = AgentConfig.from_env()
    client = LatSimClient(config)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    client.register()
    last_heartbeat = 0.0
    while True:
        if time.time() - last_heartbeat >= HEARTBEAT_SECONDS:
            client.worker_heartbeat()
            last_heartbeat = time.time()
        claim = client.claim()
        if claim is None:
            time.sleep(POLL_SECONDS)
            continue
        try:
            run_claim(client, claim)
        except (OSError, RuntimeError, ValueError) as exc:
            try:
                client.report_failure(claim, "E901", "AGENT", str(exc))
            except RuntimeError:
                pass


if __name__ == "__main__":
    main()
