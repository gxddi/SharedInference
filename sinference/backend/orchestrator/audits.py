"""
Audits: a hidden sample of jobs keeps what crossed each boundary between slices, for the verifier.

Every job hashes its boundaries (the hashes go in its record, and so on chain);
audited jobs also keep the bytes, in data/audits/<job>/:

  manifest.json      model, packages, slices, sampling settings, prompt + output tokens, boundary hashes
  boundary-<k>.bin   hidden states leaving slice k, every step in order (bf16, [tokens, hidden])

The verifier replays one slice from its inputs and compares its outputs; it isn't
built yet. Its verdict goes through fraud() (or verdict.py): the owner's stake is
slashed, and their account and wallet are banned.
"""

import hashlib
import json
from pathlib import Path
import secrets
import shutil
import time

import psycopg
from psycopg.rows import dict_row

RATE = 0.1  # share of jobs audited
ROOT = Path(__file__).resolve().parents[3] / "data/audits"

# Needs the nodes backend's tables (users, nodes, slices) and the jobs tables
SCHEMA = """
CREATE TABLE IF NOT EXISTS bans (
    user_id TEXT PRIMARY KEY REFERENCES users(id), wallet TEXT,
    job_id TEXT NOT NULL, node_id TEXT NOT NULL, slash TEXT, banned_at BIGINT NOT NULL
);
"""


def picked():
    """Audit this job? Only the orchestrator knows; to a node, every job looks the same."""
    return secrets.randbelow(10**6) < RATE * 10**6


class Trace:
    """What crosses each boundary during a job: hashed always, kept on disk when audited."""

    def __init__(self, job, boundaries, audited):
        self.hashes = [hashlib.sha256() for _ in range(boundaries)]
        self.folder = ROOT / job if audited else None
        self.files = []
        if self.folder:
            self.folder.mkdir(parents=True, exist_ok=True)
            self.files = [open(self.folder / f"boundary-{k}.bin", "wb") for k in range(boundaries)]

    def add(self, boundary, payload):
        self.hashes[boundary].update(payload)
        if self.files:
            self.files[boundary].write(payload)

    def digests(self):
        return [hash.hexdigest() for hash in self.hashes]

    def close(self, manifest):
        """The job finished: audited ones keep their evidence w/ a manifest."""
        for file in self.files:
            file.close()
        if self.folder:
            (self.folder / "manifest.json").write_text(json.dumps(manifest, indent=2))

    def discard(self):
        """The job failed: nothing to pay, nothing to verify."""
        for file in self.files:
            file.close()
        if self.folder:
            shutil.rmtree(self.folder, ignore_errors=True)


class Audits:
    def __init__(self, database_url, stakes):
        self.database_url = database_url
        self.stakes = stakes

    def connect(self):
        return psycopg.connect(self.database_url, row_factory=dict_row, connect_timeout=10)

    def setup(self):
        with self.connect() as db:
            db.execute(SCHEMA)

    def banned(self, user_id, wallet):
        with self.connect() as db:
            return db.execute("SELECT 1 FROM bans WHERE user_id = %s OR (wallet IS NOT NULL AND wallet = %s)",
                              (user_id, wallet)).fetchone() is not None

    def fraud(self, job, node_id):
        """The verifier found this node's slice of an audited job wrong: slash its owner, and ban them.

        Returns the slash transaction's signature (None while Solana is off).
        """
        manifest = ROOT / job / "manifest.json"
        if not manifest.exists():
            raise ValueError(f"Job {job} wasn't audited (no evidence in {manifest.parent}).")
        evidence = hashlib.sha256(manifest.read_bytes()).digest()

        with self.connect() as db:
            part = db.execute("SELECT user_id, wallet FROM job_parts WHERE job_id = %s AND node_id = %s",
                              (job, node_id)).fetchone()
        if not part:
            raise ValueError(f"Node {node_id} didn't serve job {job}.")

        signature = self.stakes.slash(part["wallet"], part["user_id"], job, evidence) if part["wallet"] else None

        # No more work for any of the owner's nodes, now or after they rejoin
        with self.connect() as db:
            db.execute("""INSERT INTO bans VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (user_id) DO NOTHING""",
                       (part["user_id"], part["wallet"], job, node_id, signature, int(time.time())))
            db.execute("DELETE FROM slices USING nodes WHERE nodes.id = slices.node_id AND nodes.user_id = %s",
                       (part["user_id"],))
        return signature
