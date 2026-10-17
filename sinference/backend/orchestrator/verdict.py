#!/usr/bin/env python3
"""
Records that a node faked its slice of an audited job: burns its owner's stake on Solana, and bans
their account + wallet.

    sinference/.venv/bin/python sinference/backend/orchestrator/verdict.py JOB NODE

This is where the verifier's verdicts go; the verifier itself (replaying a slice
from data/audits/JOB/) isn't built yet.
"""

import argparse
import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv

from audits import Audits
from rates import sol_usd
from stakes import StakeError, Stakes


def main():
    load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False, interpolate=False)  # sinference/.env

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("job", help="The audited job's id")
    parser.add_argument("node", help="The node whose slice was wrong")
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        parser.error("Set DATABASE_URL in sinference/.env")

    try:
        signature = Audits(database_url, Stakes.from_env(sol_usd)).fraud(args.job, args.node)
    except (OSError, ValueError, StakeError, psycopg.Error) as exc:
        parser.exit(1, f"{exc}\n")

    print(f"Banned the owner of node {args.node}.")
    print(f"Slashed: {signature}" if signature else "Nothing slashed (Solana is off, or they have no wallet).")


if __name__ == "__main__":
    main()
