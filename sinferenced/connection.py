"""
The node's open connection to the server (sinference/backend/nodes/connections.py).

The nodes API says where to connect; then it's frames.py messages both ways:

  node → service   hello, heartbeat (every HEARTBEAT seconds), join, ready, unloaded,
                   and the worker's hidden / token / error replies
  service → node   welcome (w/ the chosen model), model (when the owner picks one),
                   slice (the layers to load), no_slice, error,
                   and forward / end for the worker

See docs/model-slices.md and docs/inference.md for the whole flow.
"""

import os
import select
import socket
import ssl
import time

from account import ClientError
from frames import Reader, pack
from hardware import gpu, gpu_memory
from supervisor import Worker

HEARTBEAT = 15              # seconds, same as the node service
TIMEOUT = 3 * HEARTBEAT     # no reply for this long = connection is dead


class NodeConnection:
    """frames.py messages over a socket."""

    def __init__(self, sock):
        self.sock = sock
        self.reader = Reader()

    def send(self, message, payload=b""):
        self.sock.sendall(pack(message, payload))

    def pending(self):
        """TLS can already hold decrypted bytes that select() can't see."""
        return isinstance(self.sock, ssl.SSLSocket) and self.sock.pending() > 0

    def read(self):
        """Read what's arrived; every whole message so far."""
        data = self.sock.recv(2**20)
        if not data:
            raise ConnectionError("closed by the node service")
        self.reader.feed(data)
        while self.pending():
            self.reader.feed(self.sock.recv(self.sock.pending()))
        return self.reader.messages()


def ready(slice):
    return {"type": "ready", "model": slice["model"], "start_layer": slice["start_layer"], "end_layer": slice["end_layer"]}


class Node:
    """This node across reconnects: its model, and the worker holding its slice."""

    def __init__(self, client, name):
        self.client = client
        self.name = name
        self.model = None
        self.worker = Worker()

    def on_message(self, conn, message, payload):
        kind = message.get("type")

        # A step of a job, for the worker; no slice to run it on → error back
        if kind in ("forward", "end"):
            if not self.worker.send(message, payload) and kind == "forward":
                conn.send({"type": "error", "job": message.get("job"), "message": "No slice loaded."})

        elif kind == "error":
            raise ClientError(message.get("message", "The node service refused this node."), 401)

        elif kind == "welcome":
            print(f"Connected as {self.name}.", flush=True)
            self.choose(conn, message)

        elif kind == "model":
            self.choose(conn, message)

        # Our layers: load them, unless we already hold exactly these (after a reconnect)
        elif kind == "slice":
            if self.worker.holds(message):
                conn.send(ready(message))
            else:
                print(f"Loading layers {message['start_layer']}–{message['end_layer'] - 1} of {message['model']} "
                      f"({message['layers']} layers in all)…", flush=True)
                self.worker.start(message, os.environ.get("SINFERENCE_DEVICE") or gpu()[0])  # env: a dev override

        elif kind == "no_slice":
            print(f"No slice for now: {message.get('message')}", flush=True)

    def choose(self, conn, message):
        """A model was picked (or we reconnected): ask the orchestrator for a slice of it."""
        model = message.get("model")
        if not model:
            print(f"Choose a model in your browser:\n\n  {self.client.server}/nodes/model/?node={message['node_id']}\n",
                  flush=True)
            return

        # Switching models frees the old slice first
        if model != self.model and self.worker.stop():
            conn.send({"type": "unloaded"})
        self.model = model
        print(f"Model: {model}. Asking for a slice…", flush=True)
        conn.send({"type": "join"})

    def check_worker(self, conn):
        loading = self.worker.state == "loading"
        for message, payload in self.worker.poll():
            kind = message.get("type")

            if kind == "ready":
                print(f"Ready: holding layers {self.worker.slice['start_layer']}–{self.worker.slice['end_layer'] - 1}.",
                      flush=True)
                conn.send(ready(self.worker.slice))
            elif kind == "failed":
                what = "Couldn't load the slice" if loading else "The worker stopped"
                print(f"{what}: {self.worker.error}", flush=True)
                self.worker.stop()
                conn.send({"type": "unloaded"})
                return
            else:
                conn.send(message, payload)  # a job's hidden states, token, or error


def stay_connected(client, config, name):
    """Keep this node connected until interrupted, reconnecting w/ backoff."""
    node = Node(client, name)
    delay = 1

    try:
        while True:
            try:
                # Ask the nodes API where to connect, every time (it may move us)
                address = client.request("/nodes/connect", token=config["access_token"])
                host, port = address["host"], address["port"]

                # TLS unless the site itself is local plain HTTP
                sock = socket.create_connection((host, port), timeout=TIMEOUT)
                if client.server.startswith("https:"):
                    sock = client.context.wrap_socket(sock, server_hostname=host)

                with sock:
                    conn = NodeConnection(sock)
                    total, free = gpu_memory()
                    conn.send({"type": "hello", "token": config["access_token"], "name": name,
                               "vram_total_mb": total, "vram_free_mb": free})
                    delay = 1

                    # The server and the worker as they speak, the worker every second while it loads
                    # (in case it dies), heartbeats when due
                    next_heartbeat = time.monotonic() + HEARTBEAT
                    while True:
                        wait = max(0, next_heartbeat - time.monotonic())
                        if node.worker.state == "loading":
                            wait = min(wait, 1)

                        sources = [sock] + ([node.worker.fileno()] if node.worker.process else [])
                        readable = [sock] if conn.pending() else select.select(sources, [], [], wait)[0]
                        if sock in readable:
                            for message, payload in conn.read():
                                node.on_message(conn, message, payload)
                        node.check_worker(conn)

                        if time.monotonic() >= next_heartbeat:
                            conn.send({"type": "heartbeat", "vram_free_mb": gpu_memory()[1]})
                            next_heartbeat = time.monotonic() + HEARTBEAT

            except ClientError as exc:
                if exc.status == 401:
                    raise  # signed out or expired: reconnecting won't help
                print(f"Can't reach the nodes API ({exc}). Retrying in {delay} s…", flush=True)
            except (OSError, ValueError, KeyError) as exc:
                print(f"Lost the node connection ({exc}). Reconnecting in {delay} s…", flush=True)

            time.sleep(delay)
            delay = min(delay * 2, 60)

    finally:
        node.worker.stop()
