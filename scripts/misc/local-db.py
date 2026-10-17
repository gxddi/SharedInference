#!/usr/bin/env python3
"""Manage the project's native PostgreSQL/TimescaleDB development database."""

import argparse
import getpass
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
from urllib.parse import quote

from dotenv import dotenv_values, set_key
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

ROOT = Path(__file__).resolve().parents[2]
BIN = ROOT / "sinference/.local/postgres/bin"
DATA = ROOT / "data/postgres"
SOCKET = ROOT / "data/postgres-socket"
ENV = ROOT / "sinference/.env"
PORT = 6543
NAME = "sharedinference"


def control(*arguments, check=True):
    return subprocess.run([str(BIN / "pg_ctl"), "-D", str(DATA), *arguments], check=check)


def start():
    if not (DATA / "PG_VERSION").is_file():
        raise RuntimeError("Run setup first: bash scripts/misc/setup-local-db.sh")
    if subprocess.run([str(BIN / "pg_ctl"), "-D", str(DATA), "status"],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        print("Local database is already running.")
        return
    SOCKET.mkdir(mode=0o700, parents=True, exist_ok=True)
    control("-l", str(ROOT / "data/postgres.log"), "-w", "start")


def initialize():
    DATA.parent.mkdir(parents=True, exist_ok=True)
    SOCKET.mkdir(mode=0o700, exist_ok=True)
    if not (DATA / "PG_VERSION").exists():
        # Unix socket access uses the current operating-system identity. TCP
        # connections require a password, including for the application role.
        with tempfile.NamedTemporaryFile(mode="w", prefix="si-db-password-") as password_file:
            password_file.write(secrets.token_urlsafe(32))
            password_file.flush()
            subprocess.run([str(BIN / "initdb"), "-D", str(DATA), "-U", getpass.getuser(),
                            "--auth-local=peer", "--auth-host=scram-sha-256", "--encoding=UTF8",
                            "--locale=C", "--pwfile=" + password_file.name], check=True)
        config = (
            "\n# Shared Inference local development\n"
            "listen_addresses = '127.0.0.1'\n"
            f"port = {PORT}\n"
            "unix_socket_directories = '" + str(SOCKET).replace("'", "''") + "'\n"
            "shared_preload_libraries = 'timescaledb'\n"
            "timescaledb.telemetry_level = 'off'\n"
            "max_connections = 30\n"
            "shared_buffers = '64MB'\n"
            "timescaledb.max_background_workers = 4\n"
            "max_worker_processes = 8\n"
        )
        with (DATA / "postgresql.conf").open("a") as output:
            output.write(config)
    start()
    existing = dotenv_values(ENV, interpolate=False).get("DATABASE_URL")
    previous = conninfo_to_dict(existing) if existing else {}
    is_local = (previous.get("host") == "127.0.0.1" and previous.get("port") == str(PORT)
                and previous.get("dbname") == NAME and previous.get("user") == NAME)
    password = previous.get("password") if is_local else None
    password = password or secrets.token_urlsafe(32)
    backup = ENV.with_name(".env.before-local")
    if existing and not is_local and backup.exists():
        raise RuntimeError("An .env.before-local backup already exists; preserve it before switching connections.")
    with psycopg.connect(host=str(SOCKET), port=PORT, user=getpass.getuser(),
                         dbname="postgres", autocommit=True) as admin:
        exists = admin.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (NAME,)).fetchone()
        operation = "ALTER ROLE" if exists else "CREATE ROLE"
        admin.execute(sql.SQL(operation + " {} LOGIN PASSWORD {}").format(
            sql.Identifier(NAME), sql.Literal(password)))
        if not admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (NAME,)).fetchone():
            admin.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(NAME), sql.Identifier(NAME)))
    with psycopg.connect(host=str(SOCKET), port=PORT, user=getpass.getuser(),
                         dbname=NAME, autocommit=True) as admin:
        admin.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
        version = admin.execute("SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'").fetchone()[0]
    url = f"postgresql://{NAME}:{quote(password, safe='')}@127.0.0.1:{PORT}/{NAME}"
    if existing and not is_local:
        with backup.open("x", opener=lambda path, flags: os.open(path, flags, 0o600)) as output:
            output.write(ENV.read_text())
    set_key(ENV, "DATABASE_URL", url, quote_mode="always")
    ENV.chmod(0o600)
    print(f"TimescaleDB {version} is ready on 127.0.0.1:{PORT}.")
    print("Updated sinference/.env; the connection password was not printed.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "start", "stop", "status"))
    arguments = parser.parse_args()
    if not (BIN / "pg_ctl").is_file():
        parser.error("Native PostgreSQL is missing. Run bash scripts/misc/setup-local-db.sh first.")
    try:
        if arguments.command == "init":
            initialize()
        elif arguments.command == "start":
            start()
        elif arguments.command == "stop":
            control("-m", "fast", "-w", "stop")
        else:
            return control("status", check=False).returncode
    except (OSError, RuntimeError, psycopg.Error, subprocess.CalledProcessError) as error:
        if isinstance(error, RuntimeError):
            parser.exit(1, str(error) + "\n")
        parser.exit(1, "Local database operation failed; inspect data/postgres.log.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
