"""
Every job: who served which layers of what, for how many tokens, and its payout.

A job is worth its tokens at its model's prices (the catalog's, in USD per million
tokens), in lamports at SOL's price then. Its nodes split that by weight: layers ×
speed in layers per second, so nodes equally fast per layer are paid by slice
size, and equal slices by speed.

Earnings go back to the node owner's wallet once their audits are past, and a stake's
collateral once nothing's left for it to back (releases).

A finished job's record is canonical JSON. Its SHA-256 goes on chain w/ the
payout, so anyone shown the record can check that it's the one that was paid.
"""

import hashlib
import json
import time

import psycopg
from psycopg.rows import dict_row

LAMPORTS = 10**9  # per SOL

# Needs auth.py's users table (and reads the nodes backend's models)
SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), model TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'done', 'failed')),
    prompt_tokens INTEGER NOT NULL, completion_tokens INTEGER NOT NULL DEFAULT 0,
    audited BOOLEAN NOT NULL, record TEXT, payout TEXT,
    created_at BIGINT NOT NULL, finished_at BIGINT
);
CREATE TABLE IF NOT EXISTS job_parts (
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE, node_id TEXT NOT NULL,
    user_id TEXT NOT NULL, wallet TEXT, start_layer INTEGER NOT NULL, end_layer INTEGER NOT NULL,
    lamports BIGINT NOT NULL DEFAULT 0, released TEXT, PRIMARY KEY (job_id, node_id)
);
CREATE TABLE IF NOT EXISTS stakes (
    wallet TEXT NOT NULL, user_id TEXT NOT NULL, emptied BOOLEAN NOT NULL DEFAULT false,
    PRIMARY KEY (wallet, user_id)
);
"""


def digest(value):
    return hashlib.sha256(json.dumps(value, separators=(",", ":")).encode()).hexdigest()


def split(lamports, parts, seconds):
    """Each node's share of a job: {node_id: lamports}, by layers × layers per second.

    parts: [{node_id, start_layer, end_layer}]; seconds: node_id → its time on the job's steps
    """
    weights = {part["node_id"]: (part["end_layer"] - part["start_layer"]) ** 2 / max(seconds.get(part["node_id"], 0), 1e-6)
               for part in parts}
    return {node_id: int(lamports * weight / sum(weights.values())) for node_id, weight in weights.items()}


class Jobs:
    def __init__(self, database_url, sol_usd):
        self.database_url = database_url
        self.sol_usd = sol_usd  # → USD per SOL, or None (rates.sol_usd)

    def connect(self):
        return psycopg.connect(self.database_url, row_factory=dict_row, connect_timeout=10)

    def setup(self):
        with self.connect() as db:
            db.execute(SCHEMA)
            db.execute("ALTER TABLE job_parts ADD COLUMN IF NOT EXISTS released TEXT")  # tables from before releases
            db.execute("UPDATE jobs SET status = 'failed' WHERE status = 'running'")  # cut short by a restart

    def start(self, job, user_id, model, parts, prompt_tokens, audited):
        """parts: the copy's slices (slices.Slices.replica)"""
        with self.connect() as db:
            db.execute("INSERT INTO jobs (id, user_id, model, status, prompt_tokens, audited, created_at) "
                       "VALUES (%s, %s, %s, 'running', %s, %s, %s)", (job, user_id, model, prompt_tokens, audited,
                                                                     int(time.time())))
            with db.cursor() as cursor:
                cursor.executemany("INSERT INTO job_parts (job_id, node_id, user_id, wallet, start_layer, end_layer) "
                                   "VALUES (%s, %s, %s, %s, %s, %s)",
                                   [(job, part["node_id"], part["user_id"], part["wallet"], part["start_layer"],
                                     part["end_layer"]) for part in parts])

    def finish(self, job, model, layers, prompt, output, boundaries, seconds):
        """Done: the job's worth is split between its nodes, and its record is set.

        seconds: node_id → its time on the job's steps (pipeline.run)
        """
        rate = self.sol_usd()
        with self.connect() as db:
            price = db.execute("SELECT prompt_price, completion_price FROM models WHERE id = %s", (model,)).fetchone()
            usd = (len(prompt) * price["prompt_price"] + len(output) * price["completion_price"]) / 10**6 if price else 0
            worth = round(usd / rate * LAMPORTS) if rate else 0
            if usd and not rate:
                print(f"No SOL price, so job {job} pays nothing.", flush=True)

            parts = db.execute("SELECT * FROM job_parts WHERE job_id = %s ORDER BY start_layer", (job,)).fetchall()
            shares = split(worth, parts, seconds)
            for part in parts:
                part["lamports"] = shares[part["node_id"]]
                db.execute("UPDATE job_parts SET lamports = %s WHERE job_id = %s AND node_id = %s",
                           (part["lamports"], job, part["node_id"]))

            record = json.dumps({
                "job": job, "model": model, "layers": layers, "usd": usd, "sol_usd": rate, "lamports": worth,
                "tokens": {"prompt": len(prompt), "completion": len(output)},
                "prompt": digest(prompt), "output": digest(output), "boundaries": boundaries,
                "parts": [{"node": part["node_id"], "owner": part["user_id"], "wallet": part["wallet"],
                           "layers": [part["start_layer"], part["end_layer"]],
                           "seconds": round(seconds.get(part["node_id"], 0), 3), "lamports": part["lamports"]}
                          for part in parts],
            }, sort_keys=True, separators=(",", ":"))
            db.execute("""UPDATE jobs SET status = 'done', completion_tokens = %s, record = %s, finished_at = %s
                WHERE id = %s""", (len(output), record, int(time.time()), job))

    def fail(self, job):
        with self.connect() as db:
            db.execute("UPDATE jobs SET status = 'failed', finished_at = %s WHERE id = %s", (int(time.time()), job))


    ###########
    # Payouts #
    ###########

    def unpaid(self, limit=20):
        """Finished jobs w/ someone to pay: [{id, record, payees: [{wallet, user_id, lamports}]}]"""
        with self.connect() as db:
            jobs = db.execute("""SELECT id, record FROM jobs WHERE status = 'done' AND payout IS NULL
                AND EXISTS (SELECT 1 FROM job_parts WHERE job_id = jobs.id AND wallet IS NOT NULL AND lamports > 0
                    AND user_id NOT IN (SELECT user_id FROM bans))
                ORDER BY finished_at LIMIT %s""", (limit,)).fetchall()
            for job in jobs:
                job["payees"] = db.execute("""SELECT wallet, user_id, lamports FROM job_parts
                    WHERE job_id = %s AND wallet IS NOT NULL AND lamports > 0
                    AND user_id NOT IN (SELECT user_id FROM bans)""", (job["id"],)).fetchall()
        return jobs

    def paid(self, job, signature):
        with self.connect() as db:
            db.execute("UPDATE jobs SET payout = %s WHERE id = %s", (signature, job))


    ############
    # Releases #
    ############

    def staking(self, wallet, user_id):
        """An account's stake backs its nodes (again): its collateral goes back once they've left."""
        with self.connect() as db:
            db.execute("""INSERT INTO stakes VALUES (%s, %s, false)
                ON CONFLICT (wallet, user_id) DO UPDATE SET emptied = false""", (wallet, user_id))

    def idle(self, before):
        """Stakes w/ nothing left to back since `before` (unix time; audits since are past): [{wallet, user_id}]

        The account's nodes all left, or it uses another wallet now, and no job paid into it is newer.
        """
        with self.connect() as db:
            return db.execute("""SELECT stakes.wallet, stakes.user_id FROM stakes
                LEFT JOIN wallets ON wallets.user_id = stakes.user_id AND wallets.address = stakes.wallet
                WHERE NOT stakes.emptied AND stakes.user_id NOT IN (SELECT user_id FROM bans)
                AND NOT EXISTS (SELECT 1 FROM job_parts JOIN jobs ON jobs.id = job_parts.job_id
                    WHERE job_parts.wallet = stakes.wallet AND job_parts.user_id = stakes.user_id
                    AND (jobs.finished_at IS NULL OR jobs.finished_at >= %(before)s))
                AND (wallets.user_id IS NULL OR NOT EXISTS (SELECT 1 FROM nodes WHERE nodes.user_id = stakes.user_id
                    AND (nodes.status <> 'offline' OR nodes.last_seen >= %(before)s)))""", {"before": before}).fetchall()

    def emptied(self, wallet, user_id, signature):
        """Everything in a stake went back to its wallet."""
        with self.connect() as db:
            db.execute("UPDATE stakes SET emptied = true WHERE wallet = %s AND user_id = %s", (wallet, user_id))
            db.execute("UPDATE job_parts SET released = %s WHERE wallet = %s AND user_id = %s AND released IS NULL",
                       (signature, wallet, user_id))

    def earnings(self, before):
        """Earnings paid for jobs finished before `before` (their audits are past) and not released yet, per stake:
        [{wallet, user_id, lamports}]"""
        with self.connect() as db:
            return db.execute("""SELECT job_parts.wallet, job_parts.user_id, sum(job_parts.lamports)::bigint AS lamports
                FROM job_parts JOIN jobs ON jobs.id = job_parts.job_id
                WHERE jobs.payout IS NOT NULL AND jobs.finished_at < %s AND job_parts.released IS NULL
                AND job_parts.wallet IS NOT NULL AND job_parts.lamports > 0
                AND job_parts.user_id NOT IN (SELECT user_id FROM bans)
                GROUP BY job_parts.wallet, job_parts.user_id""", (before,)).fetchall()

    def released(self, wallet, user_id, before, signature):
        """Those earnings went back to the wallet."""
        with self.connect() as db:
            db.execute("""UPDATE job_parts SET released = %s FROM jobs WHERE jobs.id = job_parts.job_id
                AND job_parts.wallet = %s AND job_parts.user_id = %s AND jobs.payout IS NOT NULL
                AND jobs.finished_at < %s AND job_parts.released IS NULL""", (signature, wallet, user_id, before))
