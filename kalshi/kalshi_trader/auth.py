"""Request signing for the Kalshi trade API.

Kalshi authenticates every private request with three headers:

    KALSHI-ACCESS-KEY        the API key id shown in the Kalshi UI
    KALSHI-ACCESS-TIMESTAMP  current time in milliseconds since the epoch
    KALSHI-ACCESS-SIGNATURE  base64( sign( timestamp + METHOD + path ) )

The signed path is the full path on the host, including the ``/trade-api/v2``
prefix and excluding any query string.

Two key types are supported, matching what Kalshi issues:

* **Ed25519** (the default for keys created since September 2026): the
  message is signed directly.
* **RSA** (older keys): RSA-PSS with SHA-256, MGF1-SHA256, salt length equal
  to the digest length.
"""

from __future__ import annotations

import base64
import time
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

API_PREFIX = "/trade-api/v2"

PrivateKey = rsa.RSAPrivateKey | ed25519.Ed25519PrivateKey


def load_private_key(path: str | Path, password: bytes | None = None) -> PrivateKey:
    """Load the PEM private key that Kalshi generated alongside the API key."""
    try:
        data = Path(path).expanduser().read_bytes()
    except OSError as exc:
        raise SystemExit(f"cannot read private key {path}: {exc.strerror}") from None
    try:
        key = serialization.load_pem_private_key(data, password=password)
    except ValueError as exc:
        raise SystemExit(f"{path} is not a valid PEM private key: {exc}") from None
    if not isinstance(key, (rsa.RSAPrivateKey, ed25519.Ed25519PrivateKey)):
        raise SystemExit(f"{path}: unsupported key type {type(key).__name__}; Kalshi keys are Ed25519 or RSA")
    return key


def key_type(key: PrivateKey) -> str:
    return "ed25519" if isinstance(key, ed25519.Ed25519PrivateKey) else "rsa"


def signing_path(path: str) -> str:
    """Return the path that gets signed: prefixed, query string stripped."""
    path = path.split("?", 1)[0]
    if not path.startswith("/"):
        path = "/" + path
    if not path.startswith(API_PREFIX):
        path = API_PREFIX + path
    return path


def sign(key: PrivateKey, timestamp_ms: int, method: str, path: str) -> str:
    message = f"{timestamp_ms}{method.upper()}{signing_path(path)}".encode()
    if isinstance(key, ed25519.Ed25519PrivateKey):
        signature = key.sign(message)
    else:
        signature = key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
    return base64.b64encode(signature).decode()


def auth_headers(key_id: str, key: PrivateKey, method: str, path: str, timestamp_ms: int | None = None) -> dict[str, str]:
    if timestamp_ms is None:
        timestamp_ms = int(time.time() * 1000)
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-TIMESTAMP": str(timestamp_ms),
        "KALSHI-ACCESS-SIGNATURE": sign(key, timestamp_ms, method, path),
    }
