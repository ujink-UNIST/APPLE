import base64
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent import (
    AgentConfig,
    validate_claim,
    validate_results,
    worker_signature_headers,
    worker_signature_message,
)


def test_agent_config_requires_https_outside_localhost(monkeypatch) -> None:
    monkeypatch.setenv("LATSIM_API_URL", "http://remote.example")
    monkeypatch.setenv("LATSIM_WORKER_ID", "worker-1")
    monkeypatch.setenv("LATSIM_WORKER_KEY_ID", "key-1")
    monkeypatch.setenv("LATSIM_WORKER_PRIVATE_KEY_PATH", "missing.pem")

    with pytest.raises(ValueError, match="HTTPS"):
        AgentConfig.from_env()


def test_worker_signature_binds_method_path_and_body() -> None:
    private_key = Ed25519PrivateKey.generate()
    body = b'{"solver":"ansys_mapdl"}'

    headers = worker_signature_headers(
        "worker-1",
        "key-1",
        private_key,
        "POST",
        "/worker/register",
        body,
    )
    message = worker_signature_message(
        "POST",
        "/worker/register",
        int(headers["X-Timestamp"]),
        headers["X-Nonce"],
        headers["X-Content-SHA256"],
    )
    signature = base64.urlsafe_b64decode(
        headers["X-Signature"] + "=" * (-len(headers["X-Signature"]) % 4)
    )
    private_key.public_key().verify(signature, message)


def test_validate_claim_rejects_unknown_fields() -> None:
    claim = {
        "schema_version": 1,
        "analysis_job_id": "analysis-abc",
        "setup_job_id": "setup-abc",
        "setup_bundle_sha256": "a" * 64,
        "analysis_type": "periodic_modal",
        "timeout_seconds": 60,
        "attempt": 1,
        "lease_token": "x" * 32,
        "lease_expires_at": time.time() + 60,
        "command": "untrusted",
    }

    with pytest.raises(ValueError, match="fields"):
        validate_claim(claim)


def test_validate_results_accepts_only_analysis_schema(tmp_path: Path) -> None:
    modal = tmp_path / "modal.csv"
    modal.write_text("mode,freq\n1,0\n2,128135.464\n", encoding="utf-8")
    validate_results(modal, "periodic_modal")

    modal.write_text("mode,value\n1,0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="header"):
        validate_results(modal, "periodic_modal")
