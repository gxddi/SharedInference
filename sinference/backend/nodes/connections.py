"""Every running node's open TLS connection, plus the orchestrator's link to them.

One JSON object per line, both ways, followed by `bytes` raw bytes when it has a
payload (hidden states):

  node → service           {"type": "hello", "token", "name", "vram_total_mb", "vram_free_mb"}
                           {"type": "heartbeat", "vram_free_mb"}           every HEARTBEAT seconds
                           {"type": "join"}                                wants a slice of its model
                           {"type": "ready", "model", "start_layer", "end_layer"}   slice loaded
                           {"type": "unloaded"}                            slice dropped
                           {"type": "hidden", "job", "shape"} + bytes      a step of a job, for the
                           {"type": "token", "job", "token"}               orchestrator (see
                           {"type": "error", "job", "message"}             docs/inference.md)
  service → node           {"type": "welcome", "node_id", "model"}  or  {"type": "error", "message"}
                           anything passed to send(): model, slice, no_slice, forward, end, ...

  service → orchestrator   {"type": "join" | "ready" | "unloaded" | "offline", "node_id", ...}
                           a node's hidden / token / error, w/ its "node_id" added
  orchestrator → service   {"type": "send", "node_id", "message"} (+ bytes, passed on w/ the message)
"""

import asyncio
import json

import psycopg

HEARTBEAT = 15              # seconds, same as sinferenced
TIMEOUT = 3 * HEARTBEAT     # silent this long = dropped
MAX_HEADER = 2**20          # a long prompt's tokens ride in the header
MAX_PAYLOAD = 512 * 2**20   # a long prompt's hidden states


def megabytes(message, key):
    value = message.get(key)
    if value is not None and (type(value) is not int or not 0 <= value < 2**31):
        raise ValueError(f"invalid {key}")
    return value


def layer(message, key):
    value = message.get(key)
    if type(value) is not int or not 0 <= value < 100000:
        raise ValueError(f"invalid {key}")
    return value


def job(message):
    value = message.get("job")
    if not isinstance(value, str) or not 0 < len(value) <= 64:
        raise ValueError("invalid job")
    return value


def step(message):
    """A node's reply for a step of a job, checked before it goes to the orchestrator."""
    kind = message["type"]
    reply = {"type": kind, "job": job(message)}

    if kind == "hidden":
        shape = message.get("shape")
        if not (isinstance(shape, list) and len(shape) == 2 and all(type(n) is int and 0 < n < 2**20 for n in shape)):
            raise ValueError("invalid shape")
        reply["shape"] = shape
    elif kind == "token":
        if type(message.get("token")) is not int or not 0 <= message["token"] < 2**31:
            raise ValueError("invalid token")
        reply["token"] = message["token"]
    else:
        reply["message"] = str(message.get("message"))[:500]
    return reply


async def receive(reader, max_payload=MAX_PAYLOAD):
    """(message, payload)"""
    line = await reader.readline()
    if not line:
        raise ConnectionError("connection closed")
    message = json.loads(line)
    if not isinstance(message, dict):
        raise ValueError("expected a JSON object")

    size = message.get("bytes", 0)
    if type(size) is not int or not 0 <= size <= max_payload:
        raise ValueError("invalid payload size")
    return message, await reader.readexactly(size) if size else b""


def write(writer, message, payload=b""):
    if payload:
        message = {**message, "bytes": len(payload)}
    writer.write(json.dumps(message).encode() + b"\n" + payload)


class NodeService:
    def __init__(self, registry):
        self.registry = registry
        self.connections = {}     # node id → writer of its open connection
        self.orchestrator = None  # writer of the orchestrator's link, once it dials in
        self.link = None          # the link's listening server
        self.loop = None

    async def start(self, host, port, context, link_port=None):
        """Accept nodes on host:port (TLS), and the orchestrator on loopback link_port."""
        self.loop = asyncio.get_running_loop()
        nodes = await asyncio.start_server(self.handle, host, port, ssl=context, limit=MAX_HEADER)
        if link_port is not None:
            self.link = await asyncio.start_server(self.handle_orchestrator, "127.0.0.1", link_port, limit=MAX_HEADER)
        return nodes


    ###########
    # Sending #
    ###########

    def send(self, node_id, message, payload=b""):
        """Send a message to a connected node (call on this loop). False if it isn't connected."""
        writer = self.connections.get(node_id)
        if writer:
            write(writer, message, payload)
        return writer is not None

    def send_threadsafe(self, node_id, message):
        """send() from another thread, e.g. the API's."""
        self.loop.call_soon_threadsafe(self.send, node_id, message)

    def tell_orchestrator(self, message, payload=b""):
        if self.orchestrator:
            write(self.orchestrator, message, payload)
        else:
            print(f"No orchestrator connected; dropped {message['type']} from {message['node_id']}.", flush=True)


    #########
    # Nodes #
    #########

    async def handle(self, reader, writer):
        ip = writer.get_extra_info("peername")[0]  # the node's address, as seen by us
        node_id = None

        try:
            # Hello: who is this, and is their session still good?
            hello, _ = await asyncio.wait_for(receive(reader, 0), 10)  # nothing big before signing in
            token, name = hello.get("token"), hello.get("name")
            if (hello.get("type") != "hello" or not isinstance(token, str) or not isinstance(name, str)
                    or not 0 < len(name) <= 64 or any(ord(c) < 32 or ord(c) == 127 for c in name)):
                raise ValueError("invalid hello")

            node = await asyncio.to_thread(self.registry.connected, token, name, ip,
                                           megabytes(hello, "vram_total_mb"), megabytes(hello, "vram_free_mb"))
            if not node:
                write(writer, {"type": "error", "message": "Session expired or revoked. Run sinference again."})
                return

            # Same node reconnecting? Keep only the newest connection
            node_id = node["id"]
            if previous := self.connections.get(node_id):
                previous.close()
            self.connections[node_id] = writer

            print(f"Node {name} connected from {ip}.", flush=True)
            write(writer, {"type": "welcome", "node_id": node_id, "model": node["model"]})

            # Messages until the node leaves, goes quiet, or loses its session
            while True:
                message, payload = await asyncio.wait_for(receive(reader), TIMEOUT)
                kind = message.get("type")

                if kind == "heartbeat":
                    if not await asyncio.to_thread(self.registry.heartbeat, node_id, megabytes(message, "vram_free_mb")):
                        write(writer, {"type": "error", "message": "Session expired or revoked. Run sinference again."})
                        return

                elif kind == "join":
                    self.tell_orchestrator({"type": "join", "node_id": node_id})

                elif kind == "ready":
                    await asyncio.to_thread(self.registry.set_status, node_id, "ready")
                    self.tell_orchestrator({"type": "ready", "node_id": node_id, "model": str(message.get("model")),
                                            "start_layer": layer(message, "start_layer"),
                                            "end_layer": layer(message, "end_layer")})

                elif kind == "unloaded":
                    await asyncio.to_thread(self.registry.set_status, node_id, "online")
                    self.tell_orchestrator({"type": "unloaded", "node_id": node_id})

                # A step of a job: straight on to the orchestrator
                elif kind in ("hidden", "token", "error"):
                    self.tell_orchestrator({**step(message), "node_id": node_id}, payload if kind == "hidden" else b"")
                    try:
                        if self.orchestrator:
                            await self.orchestrator.drain()
                    except OSError:
                        pass  # the orchestrator left; it fails its jobs itself

                else:
                    raise ValueError("unexpected message")

        except (OSError, ValueError, TimeoutError, asyncio.IncompleteReadError, psycopg.Error):
            pass  # bad, silent, or failed connections are dropped; the node reconnects

        finally:
            # Only the newest connection marks the node offline
            if node_id and self.connections.get(node_id) is writer:
                del self.connections[node_id]
                try:
                    await asyncio.to_thread(self.registry.set_status, node_id, "offline")
                except psycopg.Error:
                    pass  # fixed on the next restart instead
                self.tell_orchestrator({"type": "offline", "node_id": node_id})
                print(f"Node {node_id} disconnected.", flush=True)
            writer.close()


    ################
    # Orchestrator #
    ################

    async def handle_orchestrator(self, reader, writer):
        """The orchestrator's link (loopback only): it sends to nodes through here."""
        if self.orchestrator:
            self.orchestrator.close()  # newest orchestrator wins
        self.orchestrator = writer
        print("Orchestrator connected.", flush=True)

        try:
            while True:
                message, payload = await receive(reader)
                node_id = str(message.get("node_id"))
                if message.get("type") == "send" and isinstance(message.get("message"), dict):
                    if self.send(node_id, message["message"], payload):
                        try:
                            await self.connections[node_id].drain()
                        except (OSError, KeyError):
                            pass  # the node left; its connection handler tells the orchestrator

        except (OSError, ValueError, asyncio.IncompleteReadError):
            pass

        finally:
            if self.orchestrator is writer:
                self.orchestrator = None
                print("Orchestrator disconnected.", flush=True)
            writer.close()
