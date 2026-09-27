import base64

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

from kalshi_trader.auth import auth_headers, load_private_key, sign, signing_path


def test_signing_path_adds_prefix_and_strips_query():
    assert signing_path("/markets?limit=5") == "/trade-api/v2/markets"
    assert signing_path("portfolio/orders") == "/trade-api/v2/portfolio/orders"
    assert signing_path("/trade-api/v2/exchange/status") == "/trade-api/v2/exchange/status"


def test_signature_verifies_with_public_key(rsa_key):
    ts = 1_700_000_000_000
    sig = sign(rsa_key, ts, "get", "/portfolio/balance?x=1")
    rsa_key.public_key().verify(
        base64.b64decode(sig),
        f"{ts}GET/trade-api/v2/portfolio/balance".encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


def test_auth_headers_and_key_loading(key_file):
    key = load_private_key(key_file)
    h = auth_headers("abc", key, "POST", "/portfolio/orders", timestamp_ms=42)
    assert h["KALSHI-ACCESS-KEY"] == "abc"
    assert h["KALSHI-ACCESS-TIMESTAMP"] == "42"
    assert base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"])
