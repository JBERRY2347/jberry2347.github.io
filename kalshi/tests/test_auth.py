import base64

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding

from kalshi_trader.auth import auth_headers, key_type, load_private_key, sign, signing_path


def test_signing_path_adds_prefix_and_strips_query():
    assert signing_path("/markets?limit=5") == "/trade-api/v2/markets"
    assert signing_path("portfolio/orders") == "/trade-api/v2/portfolio/orders"
    assert signing_path("/trade-api/v2/exchange/status") == "/trade-api/v2/exchange/status"


def test_rsa_signature_verifies_with_public_key(rsa_key):
    ts = 1_700_000_000_000
    sig = sign(rsa_key, ts, "get", "/portfolio/balance?x=1")
    rsa_key.public_key().verify(
        base64.b64decode(sig),
        f"{ts}GET/trade-api/v2/portfolio/balance".encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    assert key_type(rsa_key) == "rsa"


def test_ed25519_signature_verifies_with_public_key(tmp_path):
    key = ed25519.Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    path = tmp_path / "kalshi.txt"   # Kalshi downloads Ed25519 keys as .txt
    path.write_bytes(pem)
    loaded = load_private_key(path)
    assert key_type(loaded) == "ed25519"
    ts = 1_700_000_000_000
    sig = sign(loaded, ts, "post", "/portfolio/orders")
    key.public_key().verify(base64.b64decode(sig), f"{ts}POST/trade-api/v2/portfolio/orders".encode())


def test_auth_headers_and_key_loading(key_file):
    key = load_private_key(key_file)
    h = auth_headers("abc", key, "POST", "/portfolio/orders", timestamp_ms=42)
    assert h["KALSHI-ACCESS-KEY"] == "abc"
    assert h["KALSHI-ACCESS-TIMESTAMP"] == "42"
    assert base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"])


def test_bad_key_files_give_clean_errors(tmp_path):
    with pytest.raises(SystemExit, match="cannot read"):
        load_private_key(tmp_path / "missing.pem")
    junk = tmp_path / "junk.pem"
    junk.write_text("not a key")
    with pytest.raises(SystemExit, match="not a valid PEM"):
        load_private_key(junk)
