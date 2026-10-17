"""
The OpenAI-compatible API, proxied by server.cpp at /api/v1/. Authenticated w/ API keys (auth.py).

  GET  /v1/models             models w/ a whole, ready copy on the network
  POST /v1/chat/completions   {model, messages, max_tokens, temperature, top_p, seed, stream}
"""

import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import secrets
import time
from urllib.parse import urlsplit

import psycopg
from psycopg.rows import dict_row

from inference import Unavailable
from pipeline import JobError

MAX_BODY = 2**20      # bytes in a request
MAX_TOKENS = 4096     # most a reply can be
DEFAULT_TOKENS = 1024
ROLES = ("system", "developer", "user", "assistant", "tool")


class APIError(Exception):
    def __init__(self, status, kind, message, code=None):
        self.status, self.kind, self.message, self.code = status, kind, message, code


class OpenAIAPI(ThreadingHTTPServer):
    def __init__(self, address, database_url, inference, slices):
        self.database_url = database_url
        self.inference = inference  # runs jobs
        self.slices = slices        # what's servable
        super().__init__(address, Handler)

    def connect(self):
        return psycopg.connect(self.database_url, row_factory=dict_row, connect_timeout=10)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # keep keys + prompts out of logs

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
            path = urlsplit(self.path).path.removeprefix("/api")  # /api/v1/... works locally too
            route = (self.command, path.rstrip("/"))

            if route == ("GET", "/v1/models"):
                self.signed_in()
                return self.reply(200, {"object": "list", "data": [
                    {"id": model["id"], "object": "model", "created": 0, "owned_by": "sharedinference"}
                    for model in self.server.slices.available()]})

            if route == ("POST", "/v1/chat/completions"):
                return self.chat(self.signed_in(), self.body())

            raise APIError(404, "invalid_request_error", "Unknown endpoint.")

        except APIError as exc:
            self.reply(exc.status, {"error": {"message": exc.message, "type": exc.kind, "code": exc.code}})
        except psycopg.Error:
            self.reply(503, {"error": {"message": "Storage is unavailable.", "type": "server_error", "code": None}})
        except Exception as exc:  # e.g. Hugging Face unreachable for a tokenizer
            print(f"Request failed: {exc!r}", flush=True)
            self.reply(500, {"error": {"message": "Something went wrong. Try again shortly.", "type": "server_error",
                                       "code": None}})

    def chat(self, user_id, data):
        model, messages, settings = request(data)
        stream = data.get("stream") is True
        usage = stream and isinstance(data.get("stream_options"), dict) and data["stream_options"].get("include_usage") is True
        if not self.server.slices.repo(model):
            raise APIError(404, "invalid_request_error", f"Unknown model {model}. See GET /v1/models.", "model_not_found")

        try:
            job, output = self.server.inference.start(user_id, model, messages, settings)
        except ValueError as exc:  # the model's chat template refused these messages
            raise APIError(400, "invalid_request_error", str(exc), "invalid_messages") from None
        except Unavailable:
            raise APIError(503, "server_error", f"No nodes are serving {model} right now. Try again shortly.",
                           "model_unavailable") from None
        base = {"id": f"chatcmpl-{job}", "created": int(time.time()), "model": model}

        # Whole reply at once
        if not stream:
            try:
                *pieces, summary = output
            except JobError as exc:
                raise APIError(502, "server_error", str(exc), "node_failed") from None
            return self.reply(200, {**base, "object": "chat.completion", "usage": summary["usage"], "choices": [
                {"index": 0, "message": {"role": "assistant", "content": "".join(pieces)},
                 "finish_reason": summary["finish_reason"]}]})

        # As it's generated (server-sent events)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        def event(payload):
            self.wfile.write(b"data: " + (payload if isinstance(payload, bytes) else json.dumps(payload).encode()) + b"\n\n")
            self.wfile.flush()

        def chunk(delta, finish_reason=None):
            return {**base, "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}

        try:
            event(chunk({"role": "assistant", "content": ""}))
            for piece in output:
                if isinstance(piece, str):
                    event(chunk({"content": piece}))
                else:
                    event(chunk({}, piece["finish_reason"]))
                    if usage:
                        event({**base, "object": "chat.completion.chunk", "choices": [], "usage": piece["usage"]})
        except JobError as exc:
            event({"error": {"message": str(exc), "type": "server_error", "code": "node_failed"}})
        except OSError:
            output.close()  # the client left; the job ends here
            return
        event(b"[DONE]")


    ###########
    # Helpers #
    ###########

    def signed_in(self):
        """User id behind the request's API key."""
        authorization = self.headers.get("Authorization", "")
        key = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
        with self.server.connect() as db:
            row = db.execute("UPDATE api_keys SET last_used = %s WHERE key_hash = %s RETURNING user_id",
                             (int(time.time()), hashlib.sha256(key.encode()).hexdigest())).fetchone()
        if not row:
            raise APIError(401, "authentication_error", "Invalid API key. Create one at /keys/.", "invalid_api_key")
        return row["user_id"]

    def body(self):
        if self.headers.get_content_type() != "application/json":
            raise APIError(415, "invalid_request_error", "Use application/json.")
        length = self.headers.get("Content-Length", "")
        if not length.isascii() or not length.isdigit() or not 0 < int(length) <= MAX_BODY:
            raise APIError(400, "invalid_request_error", "Invalid request length.")
        self.connection.settimeout(30)
        try:
            data = json.loads(self.rfile.read(int(length)))
        except (ValueError, TimeoutError):
            raise APIError(400, "invalid_request_error", "Invalid JSON body.") from None
        if not isinstance(data, dict):
            raise APIError(400, "invalid_request_error", "Expected a JSON object.")
        return data


def request(data):
    """(model, messages, settings) from a chat completion request; other fields are ignored."""
    def invalid(field, message):
        return APIError(400, "invalid_request_error", message, f"invalid_{field}")

    model = data.get("model")
    if not isinstance(model, str) or not model:
        raise invalid("model", "Send a model, e.g. from GET /v1/models.")

    # Messages: text only (a list of text parts is joined)
    messages = data.get("messages")
    if not isinstance(messages, list) or not 0 < len(messages) <= 512:
        raise invalid("messages", "Send 1–512 messages.")
    chat = []
    for message in messages:
        content = message.get("content", "") if isinstance(message, dict) else None
        if content is None and message.get("role") == "assistant":
            content = ""  # w/ tool calls (not supported; their text still counts)
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text")
        if not isinstance(message, dict) or message.get("role") not in ROLES or not isinstance(content, str):
            raise invalid("messages", f"Each message needs a role ({', '.join(ROLES)}) and text content.")
        chat.append({"role": "system" if message["role"] == "developer" else message["role"], "content": content})

    if data.get("n", 1) != 1:
        raise invalid("n", "Only n = 1 is supported.")

    # Sampling
    max_tokens = data.get("max_completion_tokens", data.get("max_tokens"))
    max_tokens = DEFAULT_TOKENS if max_tokens is None else max_tokens
    temperature = data.get("temperature", 1)
    top_p = data.get("top_p", 1)
    seed = data.get("seed")
    if type(max_tokens) is not int or not 0 < max_tokens <= MAX_TOKENS:
        raise invalid("max_tokens", f"max_tokens must be 1–{MAX_TOKENS}.")
    if type(temperature) not in (int, float) or not 0 <= temperature <= 2:
        raise invalid("temperature", "temperature must be 0–2.")
    if type(top_p) not in (int, float) or not 0 < top_p <= 1:
        raise invalid("top_p", "top_p must be above 0, up to 1.")
    if seed is not None and type(seed) is not int:
        raise invalid("seed", "seed must be an integer.")

    return model, chat, {"max_tokens": max_tokens, "temperature": float(temperature), "top_p": float(top_p),
                         "seed": (secrets.randbits(63) if seed is None else seed) % 2**63}
