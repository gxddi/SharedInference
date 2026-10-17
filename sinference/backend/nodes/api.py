"""The nodes API, proxied by server.cpp at /api/nodes/. Everything about nodes goes through here.

  GET  /nodes/connect   node session     → {host, port} to open the node connection on
  GET  /nodes/list      browser session  → the owner's nodes, the model catalog, their wallet + stake, Solana settings
  POST /nodes/model     browser session  → {node_id, model}, sent to the node right away
  GET  /nodes/wallet    either session   → {needed, address}: whether the account still needs a wallet
  POST /nodes/wallet    either session   → {key}: the private key of the wallet all its nodes stake from
"""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from urllib.parse import urlsplit

import psycopg
from solders.keypair import Keypair
from solders.pubkey import Pubkey

from catalog import IDS


def solana():
    """The Solana settings (see orchestrator/stakes.py), or None while it's off."""
    if not os.environ.get("SOLANA_PROGRAM"):
        return None
    return {"rpc": os.environ.get("SOLANA_RPC") or "https://api.devnet.solana.com",
            "program": os.environ["SOLANA_PROGRAM"], "min_stake_usd": float(os.environ.get("MIN_STAKE_USD") or 20)}


def keypair(key):
    """A wallet from its private key: base58, or a keypair file's [..] array."""
    try:
        return Keypair.from_bytes(bytes(json.loads(key))) if key.lstrip().startswith("[") else Keypair.from_base58_string(key)
    except (ValueError, TypeError):
        raise APIError(400, "invalid_wallet", "That isn't a Solana private key.") from None


class APIError(Exception):
    def __init__(self, status, error, message):
        self.status, self.error, self.message = status, error, message


class NodesAPI(ThreadingHTTPServer):
    def __init__(self, address, registry, service, public_url, node_address):
        self.registry = registry          # node state in PostgreSQL
        self.service = service            # live node connections
        self.public_url = public_url.rstrip("/")  # browser POSTs only from the site itself
        self.node_address = node_address  # (host, port) nodes connect to
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # keep tokens out of logs

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


    ##########
    # Routes #
    ##########

    def handle_api(self):
        try:
            path = urlsplit(self.path).path.removeprefix("/api")  # /api/nodes/... works locally too
            route = (self.command, path)

            # A node asks where to open its connection
            if route == ("GET", "/nodes/connect"):
                self.signed_in("node")
                host, port = self.server.node_address
                return self.reply(200, {"host": host, "port": port})

            # An owner lists their nodes
            if route == ("GET", "/nodes/list"):
                user_id, settings = self.signed_in("browser"), solana()
                listing = self.server.registry.nodes(user_id)
                if settings and listing["wallet"]:  # its stake, for the page to read off the chain
                    listing["stake"] = str(Pubkey.find_program_address([b"stake", bytes(Pubkey.from_string(listing["wallet"])),
                                                                         bytes.fromhex(user_id)],
                                                                        Pubkey.from_string(settings["program"]))[0])
                return self.reply(200, {**listing, "solana": settings})

            # An owner picks a model; the node hears about it straight away
            if route == ("POST", "/nodes/model"):
                user_id = self.signed_in("browser")
                data = self.body()
                node_id, model = data.get("node_id"), data.get("model")
                if not isinstance(node_id, str) or not isinstance(model, str):
                    raise APIError(400, "invalid_request", "Send a node_id and a model.")
                if model not in IDS:
                    raise APIError(400, "invalid_model", "Choose a model from the list.")
                if not self.server.registry.set_model(user_id, node_id, model):
                    raise APIError(404, "not_found", "Node not found.")

                self.server.service.send_threadsafe(node_id, {"type": "model", "node_id": node_id, "model": model})
                return self.reply(200, {"ok": True})

            # The account's wallet, from the nodes page or sinference up (whichever comes first)
            if route == ("GET", "/nodes/wallet"):
                address = self.server.registry.wallet(self.signed_in("browser", "node"))
                return self.reply(200, {"needed": solana() is not None, "address": address})

            if route == ("POST", "/nodes/wallet"):
                user_id = self.signed_in("browser", "node")
                key = self.body().get("key")
                wallet = keypair(key) if isinstance(key, str) else None
                if not wallet:
                    raise APIError(400, "invalid_wallet", "Send the wallet's private key.")
                self.server.registry.set_wallet(user_id, str(wallet.pubkey()), str(wallet))
                return self.reply(200, {"address": str(wallet.pubkey())})

            raise APIError(404, "not_found", "Unknown endpoint.")

        except APIError as exc:
            self.reply(exc.status, {"error": exc.error, "message": exc.message})
        except psycopg.Error:
            self.reply(503, {"error": "storage_unavailable", "message": "Node storage is unavailable."})


    ###########
    # Helpers #
    ###########

    def signed_in(self, *kinds):
        """User id of the session making this request, if it's one of these kinds (browser, node)."""
        user_id = self.server.registry.user(self.headers.get("Authorization", ""), *kinds)
        if not user_id:
            raise APIError(401, "unauthorized", "Please sign in again.")
        return user_id

    def body(self):
        """JSON object from a same-origin POST."""
        origin = self.headers.get("Origin")
        if origin is not None and origin != self.server.public_url:
            raise APIError(403, "invalid_origin", "Request origin is not allowed.")
        if self.headers.get_content_type() != "application/json":
            raise APIError(415, "invalid_content_type", "Use application/json.")

        length = self.headers.get("Content-Length", "")
        if not length.isascii() or not length.isdigit() or not 0 < int(length) <= 4096:
            raise APIError(400, "invalid_request", "Invalid request length.")
        self.connection.settimeout(10)
        try:
            data = json.loads(self.rfile.read(int(length)))
        except (ValueError, TimeoutError):
            raise APIError(400, "invalid_request", "Invalid JSON body.") from None
        if not isinstance(data, dict):
            raise APIError(400, "invalid_request", "Expected a JSON object.")
        return data
