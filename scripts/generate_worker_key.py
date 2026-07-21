from __future__ import annotations

import argparse
import base64
import os
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate one APPLE Ed25519 worker keypair")
    parser.add_argument("private_key_path", type=Path)
    args = parser.parse_args()
    path = args.private_key_path
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite private key: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)

    private_key = Ed25519PrivateKey.generate()
    path.write_bytes(
        private_key.private_bytes(
            Encoding.PEM,
            PrivateFormat.PKCS8,
            NoEncryption(),
        )
    )
    os.chmod(path, 0o600)
    public_bytes = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    public_key = base64.urlsafe_b64encode(public_bytes).rstrip(b"=").decode("ascii")
    print(public_key)


if __name__ == "__main__":
    main()
