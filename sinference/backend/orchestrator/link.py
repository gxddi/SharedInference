"""
The nodes backend's loopback link: how the orchestrator reaches nodes and hears from them.

A JSON line per message, then `bytes` raw bytes when it has a payload (see
nodes/connections.py for every message).
"""

import json
import threading

MAX_HEADER = 2**20
MAX_PAYLOAD = 512 * 2**20


def pack(message, payload=b""):
    if payload:
        message = {**message, "bytes": len(payload)}
    return json.dumps(message).encode() + b"\n" + payload


class Link:
    def __init__(self):
        self.sock = None
        self.lock = threading.Lock()  # API threads + the link's own thread all send

    def attach(self, sock):
        with self.lock:
            self.sock = sock

    def detach(self):
        with self.lock:
            self.sock = None

    def send(self, node_id, message, payload=b""):
        """To a node, via the nodes backend. False while unlinked."""
        with self.lock:
            if not self.sock:
                return False
            try:
                self.sock.sendall(pack({"type": "send", "node_id": node_id, "message": message}, payload))
            except OSError:
                return False
        return True

    def messages(self):
        """(message, payload) as the nodes backend sends them, until the link closes."""
        stream = self.sock.makefile("rb")
        while line := stream.readline(MAX_HEADER):
            message = json.loads(line)
            size = message.get("bytes", 0) if isinstance(message, dict) else None
            if type(size) is not int or not 0 <= size <= MAX_PAYLOAD:
                raise ValueError("invalid message from the nodes backend")
            payload = stream.read(size) if size else b""
            if len(payload) < size:
                return
            yield message, payload
