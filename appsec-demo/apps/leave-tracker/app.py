"""Acme Leave Tracker -- DEMO APP with a few realistic, moderate flaws.

Fictional HR time-off tracker used to demonstrate an AppSec review and the demo pipeline.
"""
import json
import os
import random
import sqlite3
import string
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

TOKENS = {"tok-alice": (1, "employee"), "tok-bob": (2, "employee"), "tok-hr": (9, "hr_admin")}

DB = sqlite3.connect(":memory:", check_same_thread=False)
DB.executescript(
    """
    CREATE TABLE leave (id INTEGER PRIMARY KEY, owner INTEGER, name TEXT, kind TEXT, note TEXT);
    INSERT INTO leave VALUES (1, 1, 'Alice Ng', 'vacation', 'Beach week'),
                             (2, 2, 'Bob Ortiz', 'medical', 'Surgery recovery - private');
    """
)


def new_reset_token():
    return "".join(random.choice(string.ascii_letters + string.digits) for _ in range(16))


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, body, headers=()):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in headers:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def user(self):
        return TOKENS.get(self.headers.get("Authorization", "").replace("Bearer ", ""))

    def do_GET(self):
        url = urlparse(self.path)
        args = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if url.path == "/health":
                return self.reply(200, {"status": "ok"})
            who = self.user()
            if not who:
                return self.reply(401, {"error": "authentication required"})
            if url.path == "/search":
                rows = DB.execute("SELECT id, name, kind FROM leave WHERE owner = ? AND name LIKE ?", (who[0], "%" + args.get("name", "") + "%")).fetchall()
                self.reply(200, rows)
            elif url.path.startswith("/leave/"):
                row = DB.execute("SELECT id, name, kind, note FROM leave WHERE id = ?", (int(url.path.rsplit("/", 1)[1]),)).fetchone()
                self.reply(200 if row else 404, row or {"error": "not found"})
            elif url.path == "/admin/export":
                if who[1] != "hr_admin":
                    return self.reply(403, {"error": "forbidden"})
                self.reply(200, DB.execute("SELECT * FROM leave").fetchall())
            elif url.path == "/reset":
                self.reply(200, {"token": new_reset_token()})
            else:
                self.reply(404, {"error": "not found"})
        except Exception:  # noqa: BLE001 - deliberately returns the stack trace
            self.reply(500, {"error": traceback.format_exc()})

    def do_POST(self):
        if urlparse(self.path).path == "/login":
            return self.reply(200, {"ok": True}, headers=[("Set-Cookie", "session=tok-alice; Path=/")])
        self.reply(404, {"error": "not found"})

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    HTTPServer((os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("PORT", "8082"))), Handler).serve_forever()
