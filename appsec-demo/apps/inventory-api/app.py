"""Acme Inventory API -- DEMO APP, the hardened reference.

Fictional warehouse-stock service used to demonstrate an AppSec review and the demo pipeline.
"""
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

API_KEY_HASH = hashlib.sha256(os.environ.get("INVENTORY_API_KEY", secrets.token_hex(16)).encode()).hexdigest()
EXPORTS = {"stock": "stock.csv", "low-stock": "low-stock.csv"}
MAX_QUERY = 64

DB = sqlite3.connect(":memory:", check_same_thread=False)
DB.executescript(
    """
    CREATE TABLE items (sku TEXT PRIMARY KEY, name TEXT, qty INTEGER);
    INSERT INTO items VALUES ('SKU-100', 'Pallet jack', 4), ('SKU-200', 'Label printer', 12);
    """
)

SECURITY_HEADERS = (
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'"),
    ("Cache-Control", "no-store"),
    ("Referrer-Policy", "no-referrer"),
)


def hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    return salt, hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000)


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        for key, value in SECURITY_HEADERS:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def authorised(self):
        supplied = hashlib.sha256(self.headers.get("X-Api-Key", "").encode()).hexdigest()
        return hmac.compare_digest(supplied, API_KEY_HASH)

    def do_GET(self):
        url = urlparse(self.path)
        args = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path == "/health":
            return self.reply(200, {"status": "ok"})
        if not self.authorised():
            return self.reply(401, {"error": "authentication required"})
        if url.path == "/search":
            term = args.get("q", "")[:MAX_QUERY]
            rows = DB.execute("SELECT sku, name, qty FROM items WHERE name LIKE ?", ("%" + term + "%",)).fetchall()
            return self.reply(200, rows)
        if url.path == "/export":
            name = EXPORTS.get(args.get("name", ""))
            return self.reply(200 if name else 404, {"file": name} if name else {"error": "unknown export"})
        self.reply(404, {"error": "not found"})

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    HTTPServer((os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("PORT", "8083"))), Handler).serve_forever()
