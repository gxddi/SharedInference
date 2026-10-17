"""
The orchestrator: hands nodes their model slices, runs jobs across them for the OpenAI-compatible
API, and pays the nodes on Solana.

    sinference/.venv/bin/python sinference/backend/orchestrator

It dials the nodes backend's loopback link and talks to nodes through it. See
docs/model-slices.md, docs/inference.md and docs/solana.md.
"""

import argparse
import os
from pathlib import Path
import socket
import threading
import time

import psycopg
from dotenv import load_dotenv

import layouts
from openai_api import OpenAIAPI
from audits import Audits
from chat import Chat
from inference import Inference
from jobs import Jobs
from link import Link
from pipeline import Pipeline
from rates import sol_usd
from slices import Slices
from stakes import StakeError, Stakes

HERE = Path(__file__).resolve().parent
PAYOUT_INTERVAL = 10     # seconds between payout rounds
RELEASE_INTERVAL = 60    # seconds between release rounds
AUDIT_WINDOW = 600       # seconds a job's audit may take before its SOL goes back


class Orchestrator:
    def __init__(self, slices, jobs, audits, stakes):
        self.slices, self.jobs, self.audits, self.stakes = slices, jobs, audits, stakes
        self.joining = threading.Lock()  # one at a time, so each sees the slices (+ stakes) the last one got
        self.link = Link()
        self.pipeline = Pipeline(self.link)

    def refusal(self, node_id):
        """Why a node's owner can't serve right now, or None; stakes for it from their wallet if it's short."""
        owner = self.slices.owner(node_id)
        if not owner:
            return None  # gone; assign() says so
        if self.audits.banned(owner["user_id"], owner["wallet"]):
            return "This account is banned after failing an audit."

        reason = self.stakes.cover(owner["wallet"], owner["secret"], owner["user_id"], owner["others"] + 1)
        if not reason and not self.stakes.off:
            self.jobs.staking(owner["wallet"], owner["user_id"])
        return reason

    def join(self, node_id):
        try:
            with self.joining:
                reason = self.refusal(node_id)
                slice, reason = (None, reason) if reason else self.slices.assign(node_id)
        except Exception as exc:  # e.g. Hugging Face unreachable
            slice, reason = None, f"Couldn't plan a slice ({exc}). Try again shortly."

        if slice:
            print(f"Node {node_id} gets layers {slice['start_layer']}–{slice['end_layer'] - 1} "
                  f"of {slice['model']} (copy {slice['replica']}).", flush=True)
            self.link.send(node_id, slice)
        else:
            self.link.send(node_id, {"type": "no_slice", "message": reason})

    def handle(self, event, payload):
        kind = event["type"]

        if kind in ("hidden", "token", "error"):  # a step of a job
            self.pipeline.reply(event, payload)
        elif kind == "join":  # may wait on Hugging Face + Solana, so off the link's thread
            threading.Thread(target=self.join, args=(event["node_id"],), daemon=True).start()
        elif kind == "ready":
            if self.slices.loaded(event["node_id"], event["model"], event["start_layer"], event["end_layer"]):
                print(f"Node {event['node_id']} is ready.", flush=True)
        elif kind in ("unloaded", "offline"):
            self.slices.release(event["node_id"])
            self.pipeline.lost(event["node_id"])

    def serve(self, sock):
        """One session linked to the nodes backend: catch up, then handle events until it closes."""
        self.link.attach(sock)
        try:
            for node_id in self.slices.tidy():  # anything missed while unlinked
                self.join(node_id)
            for event, payload in self.link.messages():
                self.handle(event, payload)
        finally:
            self.link.detach()

    def run(self, port):
        """Stay linked to the nodes backend, reconnecting when it restarts."""
        while True:
            try:
                with socket.create_connection(("127.0.0.1", port)) as sock:
                    print(f"Linked to the nodes backend on 127.0.0.1:{port}.", flush=True)
                    self.serve(sock)
            except (OSError, ValueError, KeyError, psycopg.Error) as exc:
                print(f"Lost the nodes backend ({exc!r}). Retrying in 2 s…", flush=True)
            time.sleep(2)


def payouts(jobs, stakes):
    """Pay finished jobs' nodes into their stakes (a transaction per job), and send SOL back to wallets once
    its audits are past: collateral nothing's left to back, and earnings."""
    last_release = 0
    while True:
        time.sleep(PAYOUT_INTERVAL)
        try:
            for job in jobs.unpaid():
                signature = stakes.pay(job["id"], job["record"], job["payees"])
                jobs.paid(job["id"], signature)
                print(f"Paid job {job['id']}: {signature}", flush=True)

            if time.time() - last_release < RELEASE_INTERVAL:
                continue
            last_release = before = time.time()
            before -= AUDIT_WINDOW

            for stake in jobs.idle(before):
                held = stakes.read(stake["wallet"], stake["user_id"])
                signature = stakes.release(stake["wallet"], stake["user_id"], held["free"]) if held and held["free"] > 0 else None
                jobs.emptied(stake["wallet"], stake["user_id"], signature)
                if signature:
                    print(f"Released {held['free']} lamports of collateral to {stake['wallet']}: {signature}", flush=True)

            for earned in jobs.earnings(before):
                signature = stakes.release(earned["wallet"], earned["user_id"], earned["lamports"])
                jobs.released(earned["wallet"], earned["user_id"], before, signature)
                print(f"Released {earned['lamports']} lamports of earnings to {earned['wallet']}: {signature}", flush=True)

        except (OSError, ValueError, StakeError, psycopg.Error) as exc:
            print(f"Payouts paused ({exc}). Retrying in {PAYOUT_INTERVAL} s…", flush=True)


def main():
    load_dotenv(HERE.parents[1] / ".env", override=False, interpolate=False)  # sinference/.env

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--link-port", type=int, default=1029, help="The nodes backend's orchestrator link")
    parser.add_argument("--api-port", type=int, default=1030, help="OpenAI-compatible API on loopback (server.cpp's /api/v1/)")
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        parser.error("Set DATABASE_URL in sinference/.env")

    try:
        stakes = Stakes.from_env(sol_usd)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Cannot load the Solana settings in sinference/.env ({exc}).\n")

    slices, jobs, audits = Slices(database_url, layouts.from_hub), Jobs(database_url, sol_usd), Audits(database_url, stakes)
    try:
        slices.setup()
        jobs.setup()
        audits.setup()
    except psycopg.Error:
        parser.exit(1, "Cannot set up the orchestrator's tables. Start auth.py and the nodes backend once first.\n")

    # The API on its own threads, payouts on another, the link on this one
    orchestrator = Orchestrator(slices, jobs, audits, stakes)
    api = OpenAIAPI(("127.0.0.1", args.api_port), database_url,
                    Inference(slices, orchestrator.pipeline, jobs, Chat()), slices)
    threading.Thread(target=api.serve_forever, daemon=True).start()
    print(f"OpenAI-compatible API: 127.0.0.1:{args.api_port}", flush=True)

    if stakes.off:
        print("Solana is off (no SOLANA_PROGRAM): nodes serve w/o a stake, and nothing is paid.", flush=True)
    else:
        threading.Thread(target=payouts, args=(jobs, stakes), daemon=True).start()
        print(f"Solana program {stakes.program} on {stakes.rpc}, vault {stakes.config}.", flush=True)

    try:
        orchestrator.run(args.link_port)
    except KeyboardInterrupt:
        pass
    finally:
        api.shutdown()
        api.server_close()


if __name__ == "__main__":
    main()
