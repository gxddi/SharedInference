#!/usr/bin/env python3
"""Accounts, node sign-in, and API keys for the site, proxied by server.cpp at /api/auth/."""

import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlencode, urlsplit

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

DEVICE_TTL = 600     # seconds a terminal sign-in link stays valid
POLL_INTERVAL = 3    # seconds between the terminal's checks
MAX_KEYS = 20        # API keys per account
SCRYPT = dict(n=2**15, r=8, p=3, maxmem=64 * 1024 * 1024)
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, created_at BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS password_credentials (
    user_id TEXT PRIMARY KEY REFERENCES users(id), salt TEXT NOT NULL, hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
    kind TEXT NOT NULL CHECK(kind IN ('browser', 'node')), expires_at BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS device_logins (
    device_hash TEXT PRIMARY KEY, user_code TEXT NOT NULL UNIQUE,
    device_name TEXT NOT NULL, expires_at BIGINT NOT NULL, last_poll BIGINT NOT NULL DEFAULT 0,
    user_id TEXT REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS rate_limits (
    bucket TEXT PRIMARY KEY, window_started_at BIGINT NOT NULL, attempts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS api_keys (
    id TEXT PRIMARY KEY, key_hash TEXT NOT NULL UNIQUE, user_id TEXT NOT NULL REFERENCES users(id),
    name TEXT NOT NULL, hint TEXT NOT NULL, created_at BIGINT NOT NULL, last_used BIGINT
);
"""


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()  # tokens/codes are only stored hashed


def password_hash(password, salt):
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), **SCRYPT).hex()


class APIError(Exception):
    def __init__(self, status, error, message):
        self.status, self.error, self.message = status, error, message


class AuthServer(ThreadingHTTPServer):
    def __init__(self, address, database_url, public_url):
        self.database_url = database_url
        self.public_url = public_url.rstrip("/")
        with self.connect() as db:
            db.execute(SCHEMA)
        super().__init__(address, Handler)

    def connect(self):
        # Commits or rolls back on exit, and always closes
        return psycopg.connect(self.database_url, row_factory=dict_row, connect_timeout=10)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # never log bodies, auth headers, or device codes

    def do_GET(self):
        self.handle_api()

    def do_POST(self):
        self.handle_api()

    def reply(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


    ###########
    # Helpers #
    ###########

    def limited(self, key, limit=12):
        """Count an attempt for key; too many in a minute → 429."""
        now = int(time.time())
        with self.server.connect() as db:
            db.execute("DELETE FROM rate_limits WHERE window_started_at < %s", (now - 60,))
            row = db.execute("""INSERT INTO rate_limits VALUES (%s, %s, 1)
                ON CONFLICT(bucket) DO UPDATE SET attempts = rate_limits.attempts + 1
                RETURNING attempts""", (digest(key), now)).fetchone()

        if row["attempts"] > limit:
            raise APIError(429, "rate_limited", "Too many attempts. Try again in a minute.")

    def session(self, db, browser=False):
        """The signed-in user behind the Authorization header (browser sessions only if asked)."""
        authorization = self.headers.get("Authorization", "")
        token = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
        row = db.execute("""SELECT users.id, users.username, sessions.kind
            FROM sessions JOIN users ON users.id = sessions.user_id
            WHERE token_hash = %s AND expires_at > %s""", (digest(token), int(time.time()))).fetchone()

        if not row or (browser and row["kind"] != "browser"):
            raise APIError(401, "unauthorized", "Please sign in again.")
        return row

    def new_session(self, db, user_id, kind):
        token = secrets.token_urlsafe(32)
        ttl = 86400 if kind == "browser" else 30 * 86400  # 1 day in a browser, 30 on a node
        db.execute("INSERT INTO sessions VALUES (%s, %s, %s, %s)",
                   (digest(token), user_id, kind, int(time.time()) + ttl))
        return token

    def text(self, data, key, maximum=256):
        value = data.get(key, "")
        if not isinstance(value, str) or not value or len(value) > maximum:
            raise APIError(400, "invalid_request", f"Invalid {key.replace('_', ' ')}.")
        return value


    ############
    # Requests #
    ############

    def handle_api(self):
        try:
            # POSTs: same-origin JSON objects only
            data = {}
            if self.command == "POST":
                origin = self.headers.get("Origin")
                if origin is not None and origin != self.server.public_url:
                    raise APIError(403, "invalid_origin", "Request origin is not allowed.")
                if self.headers.get_content_type() != "application/json":
                    raise APIError(415, "invalid_content_type", "Use application/json.")

                length = self.headers.get("Content-Length", "")
                if not length.isascii() or not length.isdigit() or not 0 < int(length) <= 8192:
                    raise APIError(400, "invalid_request", "Invalid request length.")
                self.connection.settimeout(10)
                try:
                    data = json.loads(self.rfile.read(int(length)))
                except (ValueError, TimeoutError):
                    raise APIError(400, "invalid_request", "Invalid JSON body.") from None
                if not isinstance(data, dict):
                    raise APIError(400, "invalid_request", "Expected a JSON object.")

            # /api/auth/... works too, for local dev + tests w/o server.cpp
            path = urlsplit(self.path).path
            payload = self.route(path.removeprefix("/api") if path.startswith("/api/auth/") else path, data)
            self.reply(200, payload)

        except APIError as exc:
            self.reply(exc.status, {"error": exc.error, "message": exc.message})
        except psycopg.Error:
            self.reply(503, {"error": "storage_unavailable", "message": "Account storage is unavailable."})

    def route(self, path, data):
        now = int(time.time())

        # GET /auth/me → who's signed in
        if self.command == "GET" and path == "/auth/me":
            with self.server.connect() as db:
                user = self.session(db)
            return {"username": user["username"], "kind": user["kind"]}

        # GET /auth/keys → this account's API keys (never the keys themselves)
        if self.command == "GET" and path == "/auth/keys":
            with self.server.connect() as db:
                user = self.session(db, browser=True)
                keys = db.execute("""SELECT id, name, hint, created_at, last_used FROM api_keys
                    WHERE user_id = %s ORDER BY created_at DESC""", (user["id"],)).fetchall()
            return {"keys": keys}

        if self.command != "POST":
            raise APIError(404, "not_found", "Unknown endpoint.")

        # POST /auth/signup, /auth/login → browser session
        if path in ("/auth/signup", "/auth/login"):
            username = self.text(data, "username", 32).strip().lower()
            password = self.text(data, "password", 128)
            if not re.fullmatch(r"[a-z0-9_]{3,32}", username):
                raise APIError(400, "invalid_username", "Use 3–32 letters, numbers, or underscores.")
            self.limited("account:" + username)
            self.limited("account:global", 120)

            with self.server.connect() as db:
                if path == "/auth/signup":
                    if len(password) < 12:
                        raise APIError(400, "weak_password", "Use at least 12 characters for your password.")
                    user_id, salt = secrets.token_hex(16), secrets.token_hex(16)
                    hashed = password_hash(password, salt)
                    try:
                        db.execute("INSERT INTO users VALUES (%s, %s, %s)", (user_id, username, now))
                        db.execute("INSERT INTO password_credentials VALUES (%s, %s, %s)", (user_id, salt, hashed))
                    except psycopg.errors.UniqueViolation:
                        raise APIError(409, "username_taken", "That username is already taken.") from None
                else:
                    row = db.execute("""SELECT users.id, salt, hash FROM users JOIN password_credentials
                        ON users.id = password_credentials.user_id WHERE username = %s""", (username,)).fetchone()

                    # Hash even for unknown users, so timing doesn't reveal who exists
                    hashed = password_hash(password, row["salt"] if row else "00" * 16)
                    if not row or not hmac.compare_digest(hashed, row["hash"]):
                        raise APIError(401, "invalid_credentials", "Incorrect username or password.")
                    user_id = row["id"]

                token = self.new_session(db, user_id, "browser")
            return {"access_token": token, "username": username}

        # POST /auth/logout → revoke this session
        if path == "/auth/logout":
            with self.server.connect() as db:
                self.session(db)
                db.execute("DELETE FROM sessions WHERE token_hash = %s",
                           (digest(self.headers["Authorization"][7:]),))
            return {"ok": True}


        ############
        # API keys #
        ############

        # POST /auth/keys → a new key for the OpenAI-compatible API, shown this once
        if path == "/auth/keys":
            name = self.text(data, "name", 64).strip()
            if not name or any(ord(c) < 32 or ord(c) == 127 for c in name):
                raise APIError(400, "invalid_request", "Invalid key name.")
            key = "si-" + secrets.token_urlsafe(32)
            with self.server.connect() as db:
                user = self.session(db, browser=True)
                count = db.execute("SELECT count(*) AS n FROM api_keys WHERE user_id = %s", (user["id"],)).fetchone()["n"]
                if count >= MAX_KEYS:
                    raise APIError(400, "too_many_keys", f"Revoke a key first (at most {MAX_KEYS}).")
                key_id = secrets.token_hex(8)
                db.execute("INSERT INTO api_keys (id, key_hash, user_id, name, hint, created_at) VALUES (%s, %s, %s, %s, %s, %s)",
                           (key_id, digest(key), user["id"], name, key[:6] + "…" + key[-4:], now))
            return {"id": key_id, "key": key}

        # POST /auth/keys/revoke → stop a key working
        if path == "/auth/keys/revoke":
            key_id = self.text(data, "id", 32)
            with self.server.connect() as db:
                user = self.session(db, browser=True)
                if not db.execute("DELETE FROM api_keys WHERE id = %s AND user_id = %s RETURNING id",
                                  (key_id, user["id"])).fetchone():
                    raise APIError(404, "not_found", "Key not found.")
            return {"ok": True}


        ####################
        # Terminal sign-in #
        ####################

        # POST /auth/device/start → the terminal asks for a sign-in link
        if path == "/auth/device/start":
            self.limited("device:start", 60)
            name = self.text(data, "device_name", 64)
            if any(ord(c) < 32 or ord(c) == 127 for c in name):
                raise APIError(400, "invalid_request", "Invalid device name.")

            device_code = secrets.token_urlsafe(32)  # secret, only the terminal has it
            code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8))
            user_code = code[:4] + "-" + code[4:]     # shown in the terminal + browser, ex: ABCD-2345

            with self.server.connect() as db:
                db.execute("DELETE FROM device_logins WHERE expires_at <= %s", (now,))
                db.execute("DELETE FROM sessions WHERE expires_at <= %s", (now,))
                db.execute("INSERT INTO device_logins (device_hash, user_code, device_name, expires_at) VALUES (%s, %s, %s, %s)",
                           (digest(device_code), user_code, name, now + DEVICE_TTL))

            url = self.server.public_url + "/login/"
            return {"device_code": device_code, "user_code": user_code,
                    "verification_uri": url, "verification_uri_complete": url + "?" + urlencode({"code": user_code}),
                    "expires_in": DEVICE_TTL, "interval": POLL_INTERVAL}

        # POST /auth/device/details, /auth/device/approve → the browser side
        if path in ("/auth/device/details", "/auth/device/approve"):
            code = self.text(data, "user_code", 16).upper()
            with self.server.connect() as db:
                user = self.session(db, browser=True)
                self.limited("device:approval:" + user["id"], 20)
                row = db.execute("SELECT * FROM device_logins WHERE user_code = %s AND expires_at > %s FOR UPDATE",
                                 (code, now)).fetchone()
                if not row or row["user_id"]:
                    raise APIError(400, "expired_code", "This code has expired or was already approved. Restart sinference login.")

                if path.endswith("/approve"):
                    db.execute("UPDATE device_logins SET user_id = %s WHERE user_code = %s", (user["id"], code))
            return {"device_name": row["device_name"], "user_code": code, "approved": path.endswith("/approve")}

        # POST /auth/device/token → the terminal checks if it's approved yet
        if path == "/auth/device/token":
            device_code = self.text(data, "device_code")
            with self.server.connect() as db:
                # Row lock, so two simultaneous polls can't both claim it
                row = db.execute("SELECT * FROM device_logins WHERE device_hash = %s FOR UPDATE", (digest(device_code),)).fetchone()
                if not row or row["expires_at"] <= now:
                    raise APIError(400, "expired_token", "Login expired. Run sinference login again.")

                if now - row["last_poll"] < POLL_INTERVAL:
                    result = {"error": "slow_down", "interval": POLL_INTERVAL + 2}
                elif not row["user_id"]:
                    db.execute("UPDATE device_logins SET last_poll = %s WHERE device_hash = %s", (now, digest(device_code)))
                    result = {"error": "authorization_pending"}
                else:
                    # Approved: hand out a node session, once
                    token = self.new_session(db, row["user_id"], "node")
                    username = db.execute("SELECT username FROM users WHERE id = %s", (row["user_id"],)).fetchone()["username"]
                    db.execute("DELETE FROM device_logins WHERE device_hash = %s", (digest(device_code),))
                    result = {"access_token": token, "username": username, "expires_in": 30 * 86400}
            return result

        raise APIError(404, "not_found", "Unknown endpoint.")


def main():
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False, interpolate=False)  # sinference/.env

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=1026)
    parser.add_argument("--public-url", default="https://localhost:1025")
    args = parser.parse_args()

    # The site's origin: HTTPS, or plain HTTP on loopback for dev
    url = urlsplit(args.public_url)
    if (url.scheme not in ("https", "http") or not url.netloc or url.path not in ("", "/")
            or url.query or url.fragment or url.username or url.password
            or (url.scheme == "http" and url.hostname not in ("localhost", "127.0.0.1", "::1"))):
        parser.error("--public-url must be an HTTPS origin (HTTP is allowed only on loopback for development)")

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        parser.error("Set DATABASE_URL in sinference/.env to your Tiger Data/PostgreSQL connection string")

    try:
        server = AuthServer(("127.0.0.1", args.port), database_url, args.public_url)
    except psycopg.Error:
        parser.exit(1, "Cannot initialize PostgreSQL account storage. Check DATABASE_URL and database availability.\n")

    print(f"Authentication service: 127.0.0.1:{args.port}; public origin: {server.public_url}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
