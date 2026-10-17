"""
Messages w/ an optional binary payload: a JSON line, then `bytes` raw bytes if it has any.

    {"type": "hidden", "job": "…", "shape": [12, 896], "bytes": 21504}\\n<21504 bytes>

Used on the node connection, and between the CLI and its worker.
"""

import json

MAX_HEADER = 2**20         # tokens of a long prompt ride in the header
MAX_PAYLOAD = 512 * 2**20  # hidden states of a long prompt


def pack(message, payload=b""):
    if payload:
        message = {**message, "bytes": len(payload)}
    return json.dumps(message).encode() + b"\n" + payload


def payload_size(message):
    size = message.get("bytes", 0)
    if type(size) is not int or not 0 <= size <= MAX_PAYLOAD:
        raise ValueError("invalid payload size")
    return size


class Reader:
    """Splits a stream into (message, payload) pairs as its bytes arrive."""

    def __init__(self):
        self.buffer = bytearray()
        self.header = None  # read, still waiting for its payload

    def feed(self, data):
        self.buffer += data

    def next(self):
        """The next whole (message, payload), or None until more arrives."""
        if self.header is None:
            end = self.buffer.find(b"\n")
            if end < 0:
                if len(self.buffer) > MAX_HEADER:
                    raise ValueError("message too long")
                return None
            message = json.loads(self.buffer[:end])
            del self.buffer[:end + 1]
            if not isinstance(message, dict):
                raise ValueError("invalid message")
            self.header = message, payload_size(message)

        message, size = self.header
        if len(self.buffer) < size:
            return None
        payload = bytes(self.buffer[:size])
        del self.buffer[:size]
        self.header = None
        return message, payload

    def messages(self):
        return list(iter(self.next, None))


def read(stream):
    """(message, payload) from a blocking binary stream, or None once it closes."""
    line = stream.readline(MAX_HEADER)
    if not line:
        return None
    message = json.loads(line)
    if not isinstance(message, dict):
        raise ValueError("invalid message")
    size = payload_size(message)
    payload = stream.read(size) if size else b""
    if len(payload) < size:
        return None  # closed mid-message
    return message, payload
