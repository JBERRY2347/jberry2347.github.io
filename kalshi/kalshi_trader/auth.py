"""Request signing for the Kalshi trade API.

Kalshi authenticates every private request with three headers:

    KALSHI-ACCESS-KEY        the API key id shown in the Kalshi UI
    KALSHI-ACCESS-TIMESTAMP  current time in milliseconds since the epoch
    KALSHI-ACCESS-SIGNATURE  base64( RSA-PSS-SHA256( timestamp + METHOD + path ) )

The signed path is the full path on the host, including the ``/trade-api/v2``
prefix and excluding any query string. The RSA-PSS salt length equals the
SHA-256 digest length.
"""

from __future__ import annotations

import base64
import time
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

API_PREFIX = "/trade-api/v2"


def load_private_key(path: str | Path, password: bytes | None = None) -> rsa.RSAPrivateKey:
    """Load the PEM private key that Kalshi generated alongside the API key."""
    try:
        data = Path(path).expanduser().read_bytes()
    except OSError as exc:
        raise SystemExit(f"cannot read private key {path}: {exc.strerror}") from None
    try:
        key = serialization.load_pem_private_key(data, password=password)
    except ValueError as exc:
        raise SystemExit(f"{path} is not a valid PEM private key: {exc}") from None
    if not isinstance(key, rsa.RSAPrivateKey):
        raise TypeError("Kalshi API keys must be RSA private keys")
    return key


def signing_path(path: str) -> str:
    """Return the path that gets signed: prefixed, query string stripped."""
    path = path.split("?", 1)[0]
    if not path.startswith("/"):
        path = "/" + path
    if not path.startswith(API_PREFIX):
        path = API_PREFIX + path
    return path


def sign(key: rsa.RSAPrivateKey, timestamp_ms: int, method: str, path: str) -> str:
    message = f"{timestamp_ms}{method.upper()}{signing_path(path)}".encode()
    signature = key.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode()


def auth_headers(key_id: str, key: rsa.RSAPrivateKey, method: str, path: str, timestamp_ms: int | None = None) -> dict[str, str]:
    if timestamp_ms is None:
        timestamp_ms = int(time.time() * 1000)
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-TIMESTAMP": str(timestamp_ms),
        "KALSHI-ACCESS-SIGNATURE": sign(key, timestamp_ms, method, path),
    }
