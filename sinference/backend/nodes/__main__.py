"""Runs the nodes API, node connections, and the orchestrator link together:

    sinference/.venv/bin/python sinference/backend/nodes
"""

import argparse
import asyncio
import os
from pathlib import Path
import ssl
import threading
from urllib.parse import urlsplit

import psycopg
from dotenv import load_dotenv

from api import NodesAPI
from connections import NodeService
from registry import Registry

HERE = Path(__file__).resolve().parent
CREDS = HERE.parents[2] / "site/creds"  # same cert as the site


async def serve(service, registry, args, context):
    # Node connections + the orchestrator's link on this loop...
    server = await service.start(args.host, args.port, context, args.link_port)
    print(f"Node connections: {args.host}:{args.port}", flush=True)
    print(f"Orchestrator link: 127.0.0.1:{args.link_port}", flush=True)

    # ...and the API on its own threads
    node_host = args.node_host or urlsplit(args.public_url).hostname
    api = NodesAPI(("127.0.0.1", args.api_port), registry, service, args.public_url, (node_host, args.port))
    threading.Thread(target=api.serve_forever, daemon=True).start()
    print(f"Nodes API: 127.0.0.1:{args.api_port}  (nodes are sent to {node_host}:{args.port})", flush=True)

    try:
        async with server:
            await server.serve_forever()
    finally:
        api.shutdown()
        api.server_close()


def main():
    load_dotenv(HERE.parents[1] / ".env", override=False, interpolate=False)  # sinference/.env

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--public-url", default="https://localhost:1025", help="The site's origin")
    parser.add_argument("--api-port", type=int, default=1028, help="Nodes API port on loopback (server.cpp's /api/nodes/)")
    parser.add_argument("--host", default="0.0.0.0", help="Where node connections are accepted")
    parser.add_argument("--port", type=int, default=1027, help="Node connection port")
    parser.add_argument("--node-host", help="Hostname nodes are told to connect to (default: the site's)")
    parser.add_argument("--link-port", type=int, default=1029, help="Orchestrator link port on loopback")
    parser.add_argument("--cert", type=Path, default=CREDS / "localhost+2.pem")
    parser.add_argument("--key", type=Path, default=CREDS / "localhost+2-key.pem")
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        parser.error("Set DATABASE_URL in sinference/.env")

    # TLS for node connections
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    try:
        context.load_cert_chain(args.cert, args.key)
    except OSError:
        parser.error("Cannot load the TLS certificate; pass --cert and --key")

    # Tables first
    registry = Registry(database_url)
    try:
        registry.setup()
    except psycopg.Error:
        parser.exit(1, "Cannot set up node storage. Start auth.py once first (it creates the sessions table) "
                       "and check DATABASE_URL.\n")

    try:
        asyncio.run(serve(NodeService(registry), registry, args, context))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
