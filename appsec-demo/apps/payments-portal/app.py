"""Acme Payments Portal -- DEMO APP, INTENTIONALLY VULNERABLE.

Fictional merchant-payments back office used to demonstrate an AppSec review and
the demo pipeline. Every flaw here is deliberate. Run it on localhost only.
"""
import hashlib
import json
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

JWT_SECRET = "s3cr3t-jwt-signing-key-2019"
AWS_ACCESS_KEY_ID = "AKIAIOSFODNN7EXAMPLE"
AWS_SECRET_ACCESS_KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"

DB = sqlite3.connect(":memory:", check_same_thread=False)
DB.executescript(
    """
    CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, pw_md5 TEXT, role TEXT);
    CREATE TABLE payments (id INTEGER PRIMARY KEY, merchant TEXT, amount REAL, card_last4 TEXT);
    INSERT INTO payments VALUES (1, 'Blue Bottle Books', 120.50, '4242'), (2, 'Harbor Coffee', 8.75, '1881');
    """
)
DB.execute("INSERT INTO users VALUES (1, 'admin', ?, 'admin')", (hashlib.md5(b"admin123").hexdigest(),))


def search_payments(term):
    query = f"SELECT id, merchant, amount FROM payments WHERE merchant LIKE '%{term}%'"
    return [dict(zip(("id", "merchant", "amount"), row)) for row in DB.execute(query)]


def read_export(name):
    with open(os.path.join("exports", name), "rb") as handle:
        return handle.read()


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, body, ctype="application/json", headers=()):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for key, value in headers:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        args = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if url.path == "/health":
                self.reply(200, {"status": "ok"})
            elif url.path == "/search":
                self.reply(200, search_payments(args.get("q", "")))
            elif url.path == "/download":
                self.reply(200, read_export(args.get("file", "")), "application/octet-stream")
            elif url.path == "/diag":
                os.system("nslookup " + args.get("host", "localhost"))
                self.reply(200, {"status": "done"})
            elif url.path == "/admin":
                rows = DB.execute("SELECT id, name, pw_md5, role FROM users").fetchall()
                self.reply(200, {"users": rows})
            else:
                self.reply(404, {"error": "not found"})
        except Exception as exc:  # noqa: BLE001 - deliberately leaks the error
            self.reply(500, {"error": str(exc)})

    def do_POST(self):
        if urlparse(self.path).path != "/login":
            return self.reply(404, {"error": "not found"})
        size = int(self.headers.get("Content-Length", 0))
        creds = json.loads(self.rfile.read(size) or b"{}")
        digest = hashlib.md5(creds.get("password", "").encode()).hexdigest()
        row = DB.execute("SELECT id FROM users WHERE name = ? AND pw_md5 = ?", (creds.get("user"), digest)).fetchone()
        if not row:
            return self.reply(401, {"error": "invalid credentials"})
        self.reply(200, {"ok": True}, headers=[("Set-Cookie", "session=%s-%s; Path=/" % (row[0], JWT_SECRET[:6]))])

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    HTTPServer((os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("PORT", "8081"))), Handler).serve_forever()
