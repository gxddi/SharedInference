#!/usr/bin/env python3
"""Shared Inference node client (Python 3.10+, no dependencies)."""

import argparse
import os
from pathlib import Path
import socket
import sys

from account import Client, ClientError, ensure_wallet, load_config, login, origin
from connection import stay_connected

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "sinference"
INSTALLED_SERVER = Path(__file__).resolve().parent / "server"  # the site the installer came from


def default_server():
    try:
        return INSTALLED_SERVER.read_text().strip()
    except OSError:
        return "https://localhost:1025"  # running from the repo


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=("up", "login", "status", "logout"), default="up")
    parser.add_argument("--server", help="Site to join (default: the one you installed from)")
    parser.add_argument("--device-name", default=socket.gethostname()[:64] or "My node")
    parser.add_argument("--ca-file", default=os.environ.get("SINFERENCE_CA_FILE"), help="Trusted certificate authority file")
    parser.add_argument("--config-dir", type=Path, default=CONFIG_DIR)
    args = parser.parse_args(argv)

    try:
        # Saved sign-in, and which server we're talking to
        path = args.config_dir / "credentials.json"
        config = load_config(path)
        server = origin(args.server or os.environ.get("SINFERENCE_SERVER")
                        or (config["server"] if config else default_server()))
        if config and origin(config["server"]) != server:
            if args.command in ("status", "logout"):
                raise ClientError("Saved credentials belong to another server. Use that server or a separate --config-dir.")
            raise ClientError("Sign out of the saved server or use a separate --config-dir before switching servers.")
        client = Client(server, args.ca_file)

        # logout: revoke on the server, then forget locally
        if args.command == "logout":
            if config:
                try:
                    client.request("/auth/logout", {}, config["access_token"])
                except ClientError as exc:
                    if exc.status != 401:  # already revoked is fine
                        raise
                path.unlink()
            print("Signed out.")
            return 0

        # login: sign in only
        if args.command == "login":
            if config:
                raise ClientError("Already signed in. Use sinference up, or sinference logout to switch accounts.")
            login(client, path, args.device_name)
            return 0

        # Drop a saved session the server no longer accepts
        if config:
            try:
                user = client.request("/auth/me", token=config["access_token"])
            except ClientError as exc:
                if exc.status != 401:
                    raise
                path.unlink()
                config = None
                print("Your session expired or was revoked.")

        if args.command == "status":
            print(f"Signed in as {user['username']} at {server}." if config else "Not signed in. Run sinference login.")
            return 0 if config else 1

        # up: sign in if needed, a wallet for the account's nodes if it has none, then stay connected
        if not config:
            config = login(client, path, args.device_name)
        ensure_wallet(client, config)
        stay_connected(client, config, args.device_name)

    except (ClientError, OSError) as exc:
        print(f"sinference: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
