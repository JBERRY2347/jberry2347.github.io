import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from kalshi_trader.config import RiskLimits, Settings


@pytest.fixture(scope="session")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def key_file(tmp_path, rsa_key):
    pem = rsa_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    p = tmp_path / "kalshi.pem"
    p.write_bytes(pem)
    return p


@pytest.fixture
def settings(tmp_path, key_file):
    return Settings(env="demo", api_key_id="key-123", private_key_path=str(key_file),
                    risk=RiskLimits(), state_path=tmp_path / "state.json")


class FakeResponse:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.content = json.dumps(body).encode() if body is not None else b""
        self.text = self.content.decode()

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class FakeSession:
    """Records requests and replays canned responses keyed by (METHOD, path)."""

    def __init__(self):
        self.routes = {}
        self.calls = []

    def route(self, method, path, body, status=200):
        self.routes[(method.upper(), path)] = (status, body)

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        path = url.split("/trade-api/v2", 1)[1]
        self.calls.append({"method": method, "path": path, "params": params, "json": json, "headers": headers})
        status, body = self.routes.get((method.upper(), path), (404, {"error": "no route"}))
        return FakeResponse(status, body)


@pytest.fixture
def fake_session():
    return FakeSession()
