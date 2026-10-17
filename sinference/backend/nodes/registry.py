"""The live node registry in PostgreSQL: who's connected, from where, w/ what GPU and model,
and the wallet each account's nodes stake from + get paid to."""

import hashlib
import secrets
import time

import psycopg
from psycopg.rows import dict_row

from catalog import IDS, MODELS

# Needs auth.py's users + sessions tables. A node lives as long as its session.
SCHEMA = """
CREATE TABLE IF NOT EXISTS models (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, repo TEXT NOT NULL, memory_gb REAL NOT NULL,
    prompt_price REAL NOT NULL DEFAULT 0, completion_price REAL NOT NULL DEFAULT 0  -- USD per million tokens
);
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    session_hash TEXT NOT NULL UNIQUE REFERENCES sessions(token_hash) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id), name TEXT NOT NULL, ip TEXT NOT NULL,
    vram_total_mb INTEGER, vram_free_mb INTEGER,
    model TEXT REFERENCES models(id) ON DELETE SET NULL,
    status TEXT NOT NULL CHECK (status IN ('offline', 'online', 'ready')),
    connected_at BIGINT NOT NULL, last_seen BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS wallets (
    user_id TEXT PRIMARY KEY REFERENCES users(id), address TEXT NOT NULL, linked_at BIGINT NOT NULL,
    secret TEXT  -- its private key (base58): the coordinator stakes w/ it (devnet only)
);
"""

# Tables from before nodes had a status (and models a repo) are rebuilt once
OUTDATED = """SELECT 1 FROM information_schema.columns
    WHERE table_schema = current_schema() AND table_name = 'nodes' AND column_name = 'online'"""


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()  # same hashing as auth.py


class Registry:
    def __init__(self, database_url):
        self.database_url = database_url

    def connect(self):
        return psycopg.connect(self.database_url, row_factory=dict_row, connect_timeout=10)

    def setup(self):
        with self.connect() as db:
            if db.execute(OUTDATED).fetchone():
                db.execute("DROP TABLE IF EXISTS slices, nodes, models")
            db.execute(SCHEMA)
            db.execute("""ALTER TABLE models ADD COLUMN IF NOT EXISTS prompt_price REAL NOT NULL DEFAULT 0,
                ADD COLUMN IF NOT EXISTS completion_price REAL NOT NULL DEFAULT 0""")  # tables from before prices
            db.execute("ALTER TABLE wallets ADD COLUMN IF NOT EXISTS secret TEXT")  # ...and before wallet keys
            db.execute("DELETE FROM wallets WHERE secret IS NULL")  # linked by address alone: added again w/ a key

            # Catalog: add/update what's listed, drop what isn't (nodes using it lose their pick)
            with db.cursor() as cursor:
                cursor.executemany("""INSERT INTO models (id, name, repo, memory_gb, prompt_price, completion_price)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, repo = EXCLUDED.repo,
                    memory_gb = EXCLUDED.memory_gb, prompt_price = EXCLUDED.prompt_price,
                    completion_price = EXCLUDED.completion_price""", MODELS)
            db.execute("DELETE FROM models WHERE id <> ALL(%s)", (IDS,))

            db.execute("UPDATE nodes SET status = 'offline'")  # nothing is connected yet

    def user(self, authorization, *kinds):
        """User id behind a 'Bearer <token>' header for a session of one of these kinds (browser, node), or None."""
        token = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
        with self.connect() as db:
            row = db.execute("""SELECT user_id FROM sessions WHERE token_hash = %s AND kind = ANY(%s)
                AND expires_at > %s""", (digest(token), list(kinds), int(time.time()))).fetchone()
        return row and row["user_id"]


    #########################
    # From node connections #
    #########################

    def connected(self, token, name, ip, vram_total_mb, vram_free_mb):
        """Register (or re-register) the node behind a session. None if the session isn't valid."""
        now = int(time.time())
        with self.connect() as db:
            return db.execute("""
                INSERT INTO nodes (id, session_hash, user_id, name, ip, vram_total_mb, vram_free_mb,
                                   status, connected_at, last_seen)
                SELECT %s, token_hash, user_id, %s, %s, %s, %s, 'online', %s, %s FROM sessions
                WHERE token_hash = %s AND kind = 'node' AND expires_at > %s
                ON CONFLICT (session_hash) DO UPDATE SET name = EXCLUDED.name, ip = EXCLUDED.ip,
                    vram_total_mb = EXCLUDED.vram_total_mb, vram_free_mb = EXCLUDED.vram_free_mb,
                    status = 'online', connected_at = EXCLUDED.connected_at, last_seen = EXCLUDED.last_seen
                RETURNING id, model""",
                (secrets.token_hex(16), name, ip, vram_total_mb, vram_free_mb, now, now, digest(token), now)).fetchone()

    def heartbeat(self, node_id, vram_free_mb):
        """Record a heartbeat. False once the node's session is revoked or expired."""
        now = int(time.time())
        with self.connect() as db:
            return db.execute("""UPDATE nodes SET vram_free_mb = %s, last_seen = %s
                FROM sessions WHERE nodes.id = %s AND sessions.token_hash = nodes.session_hash
                AND sessions.expires_at > %s RETURNING nodes.id""", (vram_free_mb, now, node_id, now)).fetchone() is not None

    def set_status(self, node_id, status):
        """online ↔ ready while connected; offline once it disconnects."""
        with self.connect() as db:
            db.execute("UPDATE nodes SET status = %s WHERE id = %s", (status, node_id))


    ####################
    # From node owners #
    ####################

    def nodes(self, user_id):
        """This user's nodes, the model catalog, and their wallet's address."""
        with self.connect() as db:
            nodes = db.execute("""SELECT id, name, ip, vram_total_mb, vram_free_mb, model, status, last_seen
                FROM nodes WHERE user_id = %s
                ORDER BY CASE status WHEN 'ready' THEN 0 WHEN 'online' THEN 1 ELSE 2 END, name""", (user_id,)).fetchall()
            models = db.execute("SELECT id, name, memory_gb FROM models ORDER BY memory_gb").fetchall()
            wallet = db.execute("SELECT address FROM wallets WHERE user_id = %s", (user_id,)).fetchone()
        return {"nodes": nodes, "models": models, "wallet": wallet and wallet["address"]}

    def wallet(self, user_id):
        with self.connect() as db:
            row = db.execute("SELECT address FROM wallets WHERE user_id = %s", (user_id,)).fetchone()
        return row and row["address"]

    def set_wallet(self, user_id, address, secret):
        """The wallet all this account's nodes stake from + get paid to (its stake: ["stake", wallet, account])."""
        with self.connect() as db:
            db.execute("""INSERT INTO wallets (user_id, address, linked_at, secret) VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE SET address = EXCLUDED.address, linked_at = EXCLUDED.linked_at,
                secret = EXCLUDED.secret""", (user_id, address, int(time.time()), secret))

    def set_model(self, user_id, node_id, model):
        """Change a node's model; False if it isn't this user's node."""
        with self.connect() as db:
            return db.execute("UPDATE nodes SET model = %s WHERE id = %s AND user_id = %s RETURNING id",
                              (model, node_id, user_id)).fetchone() is not None
