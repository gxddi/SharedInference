import asyncio
import concurrent.futures
from functools import partial
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import select
import selectors
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / "sinferenced"
CLIENT_SOURCE = CLIENT / "cli.py"
CLIENT_MODULES = ("cli", "account", "connection", "frames", "hardware", "supervisor", "worker")


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


app = load("auth_app", "sinference/backend/auth.py")

sys.path.insert(0, str(ROOT / "sinference/backend/nodes"))  # the nodes backend's own modules
sys.path.insert(0, str(CLIENT))                             # the node client's own modules
from connection import NodeConnection  # noqa: E402
from api import NodesAPI  # noqa: E402
from connections import NodeService  # noqa: E402
from registry import Registry  # noqa: E402


class TestConnection(NodeConnection):
    """sinferenced's node connection, w/ a blocking receive for tests."""

    def __init__(self, sock):
        super().__init__(sock)
        self.queued = []

    def receive(self, timeout, payload=False):
        """The next message (+ its payload if asked), or None if nothing arrives in time."""
        deadline = time.monotonic() + timeout
        while not self.queued:
            wait = deadline - time.monotonic()
            if wait <= 0 or not (self.pending() or select.select([self.sock], [], [], wait)[0]):
                return None
            self.queued += self.read()
        message, data = self.queued.pop(0)
        return (message, data) if payload else message


def read_until(process, pattern, timeout=8):
    """Read a running process's output until pattern matches; return the match and output."""
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    output, match = "", None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not match and process.poll() is None:
        if selector.select(timeout=.2):
            output += os.read(process.stdout.fileno(), 4096).decode()
            match = re.search(pattern, output)
    selector.close()
    return match, output


class ServiceTestCase(unittest.TestCase):
    """Runs the authentication service against an isolated PostgreSQL schema."""

    @classmethod
    def setUpClass(cls):
        cls.database_url = os.environ.get("TEST_DATABASE_URL")
        if not cls.database_url:
            raise RuntimeError("Set TEST_DATABASE_URL to a dedicated PostgreSQL test database.")

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.schema = "auth_test_" + secrets.token_hex(8)
        with psycopg.connect(self.database_url) as db:
            db.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        self.addCleanup(self.drop_schema)
        self.database = make_conninfo(self.database_url, options="-c search_path=" + self.schema)
        self.server = app.AuthServer(("127.0.0.1", 0), self.database, "http://127.0.0.1")
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.server.public_url = self.url
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.stop, thread)

    def drop_schema(self):
        with psycopg.connect(self.database_url) as db:
            db.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))

    def stop(self, thread):
        self.server.shutdown()
        self.server.server_close()
        thread.join(timeout=2)

    def request(self, path, data=None, token=None, origin=None, content_type="application/json", base=None):
        headers = {"Content-Type": content_type}
        if token:
            headers["Authorization"] = "Bearer " + token
        if origin:
            headers["Origin"] = origin
        request = Request((base or self.url) + path, headers=headers,
                          data=json.dumps(data).encode() if data is not None else None)
        try:
            response = urlopen(request, timeout=10)
        except HTTPError as exc:
            response = exc
        with response:
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            return response.code, json.load(response)

    def signup(self, username="alice"):
        status, data = self.request("/auth/signup", {"username": username, "password": "a sufficiently long password"})
        self.assertEqual(status, 200, data)
        return data["access_token"]

    def start(self):
        status, data = self.request("/auth/device/start", {"device_name": "test-node"})
        self.assertEqual(status, 200)
        self.assertNotIn(data["device_code"], data["verification_uri_complete"])
        return data

    def approve(self, device, token):
        status, data = self.request("/auth/device/approve", {"user_code": device["user_code"]}, token)
        self.assertEqual(status, 200, data)

    def start_node_service(self, link=False):
        """Run node connections (no TLS) + the nodes API on ephemeral ports, like __main__.py.

        Sets self.nodes_url (the API) and, w/ link, self.link_port; returns the node connection port.
        """
        registry = Registry(self.database)
        registry.setup()

        # Node connections on an event loop thread
        loop = asyncio.new_event_loop()
        service = NodeService(registry)
        server = loop.run_until_complete(service.start("127.0.0.1", 0, None, 0 if link else None))
        port = server.sockets[0].getsockname()[1]
        if link:
            self.link_port = service.link.sockets[0].getsockname()[1]
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()

        async def shutdown():
            server.close()
            if service.link:
                service.link.close()
            tasks = asyncio.all_tasks() - {asyncio.current_task()}
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        def stop():
            asyncio.run_coroutine_threadsafe(shutdown(), loop).result(timeout=5)
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=2)
        self.addCleanup(stop)

        # API on its own thread, sending nodes to the port above
        api = NodesAPI(("127.0.0.1", 0), registry, service, self.url, ("127.0.0.1", port))
        threading.Thread(target=api.serve_forever, daemon=True).start()
        self.addCleanup(api.server_close)
        self.addCleanup(api.shutdown)
        self.nodes_url = f"http://127.0.0.1:{api.server_port}"
        self.port = port
        return port

    def node_token(self, browser):
        """A fresh node session for the browser's user."""
        device = self.start()
        self.approve(device, browser)
        with self.server.connect() as db:
            db.execute("UPDATE device_logins SET last_poll = 0")
        return self.request("/auth/device/token", {"device_code": device["device_code"]})[1]["access_token"]

    def connect(self, token, vram_total_mb=8192, vram_free_mb=6144, name="gpu-box"):
        """A raw node connection (what sinferenced does), hello sent."""
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        self.addCleanup(sock.close)
        node = TestConnection(sock)
        node.send({"type": "hello", "token": token, "name": name,
                   "vram_total_mb": vram_total_mb, "vram_free_mb": vram_free_mb})
        return sock, node

    def start_site(self):
        """Stand-in for server.cpp: one origin, /api/auth/ → auth.py and /api/nodes/ → the nodes API."""
        upstreams = {"/api/auth/": self.url, "/api/nodes/": self.nodes_url}

        class Proxy(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.forward()

            def do_POST(self):
                self.forward()

            def forward(self):
                base = next(url for prefix, url in upstreams.items() if self.path.startswith(prefix))
                length = int(self.headers.get("Content-Length") or 0)
                headers = {key: value for key, value in self.headers.items()
                           if key.lower() in ("authorization", "content-type", "origin")}
                request = Request(base + self.path, data=self.rfile.read(length) if length else None,
                                  headers=headers, method=self.command)
                try:
                    response = urlopen(request, timeout=10)
                except HTTPError as exc:
                    response = exc
                with response:
                    body = response.read()
                    self.send_response(response.code)
                    for key in ("Content-Type", "Cache-Control"):
                        self.send_header(key, response.headers[key])
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

        site = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
        threading.Thread(target=site.serve_forever, daemon=True).start()
        self.addCleanup(site.server_close)
        self.addCleanup(site.shutdown)
        return f"http://127.0.0.1:{site.server_port}"

    def node_row(self):
        with self.server.connect() as db:
            return db.execute("SELECT * FROM nodes").fetchone()

    def wait_for(self, condition, timeout=5):
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > deadline:
                self.fail("Timed out waiting for the node service.")
            time.sleep(.05)


class AuthTests(ServiceTestCase):
    def test_signup_login_and_storage(self):
        token = self.signup("Alice")
        self.assertEqual(self.request("/auth/me", token=token)[1]["username"], "alice")
        status, login = self.request("/auth/login", {"username": "ALICE", "password": "a sufficiently long password"})
        self.assertEqual(status, 200)
        self.assertNotEqual(token, login["access_token"])
        self.assertEqual(self.request("/auth/login", {"username": "alice", "password": "wrong"})[0], 401)
        self.assertEqual(self.request("/auth/login", {"username": "nobody", "password": "wrong"})[0], 401)
        self.assertEqual(self.request("/auth/signup", {"username": "alice", "password": "another long password"})[0], 409)
        self.assertEqual(self.request("/auth/signup", {"username": "other", "password": "short"})[0], 400)
        with self.server.connect() as db:
            credential = db.execute("SELECT salt, hash FROM password_credentials").fetchone()
            sessions = db.execute("SELECT token_hash FROM sessions").fetchall()
        self.assertEqual(len(credential["salt"]), 32)
        self.assertEqual(len(credential["hash"]), 128)
        self.assertNotEqual(credential["hash"], "a sufficiently long password")
        self.assertIn(app.digest(token), [session["token_hash"] for session in sessions])
        self.assertNotIn(token, [session["token_hash"] for session in sessions])

    def test_device_requires_signin_and_explicit_approval(self):
        browser = self.signup()
        device = self.start()
        data = {"user_code": device["user_code"]}
        self.assertEqual(self.request("/auth/device/approve", data)[0], 401)
        self.assertEqual(self.request("/auth/device/details", data, browser)[1]["device_name"], "test-node")
        poll = {"device_code": device["device_code"]}
        self.assertEqual(self.request("/auth/device/token", poll)[1]["error"], "authorization_pending")
        self.assertEqual(self.request("/auth/device/token", poll)[1]["error"], "slow_down")
        self.approve(device, browser)
        with self.server.connect() as db:
            db.execute("UPDATE device_logins SET last_poll = 0")
        status, result = self.request("/auth/device/token", poll)
        self.assertEqual(status, 200)
        node = result["access_token"]
        self.assertEqual(self.request("/auth/me", token=node)[1]["kind"], "node")
        self.assertEqual(self.request("/auth/device/token", poll)[1]["error"], "expired_token")
        self.assertEqual(self.request("/auth/device/approve", data, browser)[0], 400)
        other_device = self.start()
        self.assertEqual(self.request("/auth/device/approve", {"user_code": other_device["user_code"]}, node)[0], 401)

    def test_expired_login_and_session_revocation(self):
        browser = self.signup()
        device = self.start()
        with self.server.connect() as db:
            db.execute("UPDATE device_logins SET expires_at = 0")
        self.assertEqual(self.request("/auth/device/token", {"device_code": device["device_code"]})[0], 400)
        self.assertEqual(self.request("/auth/device/approve", {"user_code": device["user_code"]}, browser)[0], 400)
        self.assertEqual(self.request("/auth/logout", {}, browser)[0], 200)
        self.assertEqual(self.request("/auth/me", token=browser)[0], 401)
        browser = self.signup("bob")
        with self.server.connect() as db:
            db.execute("UPDATE sessions SET expires_at = 0")
        self.assertEqual(self.request("/auth/me", token=browser)[0], 401)

    def test_concurrent_poll_can_only_redeem_once(self):
        browser = self.signup()
        device = self.start()
        self.approve(device, browser)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.request("/auth/device/token", {"device_code": device["device_code"]}), range(2)))
        self.assertEqual(sum("access_token" in data for _, data in results), 1)
        self.assertEqual(sorted(status for status, _ in results), [200, 400])

    def test_concurrent_approval_can_only_assign_one_owner(self):
        alice, bob = self.signup(), self.signup("bob")
        device = self.start()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda token: self.request("/auth/device/approve", {
                "user_code": device["user_code"],
            }, token), (alice, bob)))
        self.assertEqual(sorted(status for status, _ in results), [200, 400])
        winner = "alice" if results[0][0] == 200 else "bob"
        _, session = self.request("/auth/device/token", {"device_code": device["device_code"]})
        self.assertEqual(session["username"], winner)

    def test_rejects_cross_origin_and_bad_bodies(self):
        self.assertEqual(self.request("/auth/signup", {}, origin="https://other.example")[0], 403)
        self.assertEqual(self.request("/auth/signup", {}, content_type="text/plain")[0], 415)
        self.assertEqual(self.request("/auth/signup", ["wrong body"])[0], 400)
        self.assertEqual(self.request("/auth/signup", {"username": 42})[0], 400)
        self.assertEqual(self.request("/auth/device/start", {"device_name": "bad\nname"})[0], 400)
        self.assertEqual(self.request("/missing")[0], 404)

    def test_public_auth_namespace(self):
        status, session = self.request("/api/auth/signup", {
            "username": "alice", "password": "a sufficiently long password",
        }, origin=self.url)
        self.assertEqual(status, 200)
        status, user = self.request("/api/auth/me", token=session["access_token"])
        self.assertEqual(status, 200)
        self.assertEqual(user["username"], "alice")
        self.assertEqual(self.request("/api/auth/device/start", {"device_name": "test-node"})[0], 200)
        self.assertEqual(self.request("/api.app/auth/device/start", {"device_name": "test-node"})[0], 404)

    def test_rate_limits_are_persistent(self):
        for _ in range(12):
            self.server.RequestHandlerClass.limited(self.fake_handler(), "same-account")
        with self.assertRaises(app.APIError) as exc:
            self.server.RequestHandlerClass.limited(self.fake_handler(), "same-account")
        self.assertEqual(exc.exception.status, 429)
        with self.server.connect() as db:
            db.execute("UPDATE rate_limits SET window_started_at = 0")
        self.server.RequestHandlerClass.limited(self.fake_handler(), "same-account")

    def fake_handler(self):
        class Context:
            pass
        context = Context()
        context.server = self.server
        return context

    def test_real_cli_waits_for_approval_then_stays_connected(self):
        browser = self.signup()
        self.start_node_service()
        site = self.start_site()
        self.server.public_url = site  # sign-in links point at the site, like in production
        config_dir = Path(self.directory.name) / "client"
        base = [sys.executable, str(CLIENT_SOURCE)]
        process = subprocess.Popen(base + ["up", "--server", site, "--device-name", "test-node",
                                          "--config-dir", str(config_dir)],
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        code, output = read_until(process, r"Confirm code: ([A-Z0-9-]+)")
        self.assertIsNotNone(code, output)
        self.assertIsNone(process.poll(), "Client must wait for browser approval")
        self.approve({"user_code": code[1]}, browser)
        link, output = read_until(process, r"/nodes/model/\?node=(\w+)")
        self.assertIsNotNone(link, output)
        node = self.node_row()
        self.assertEqual((node["id"], node["name"], node["ip"], node["status"], node["model"]),
                         (link[1], "test-node", "127.0.0.1", "online", None))

        # A model picked in the browser reaches the terminal over the open connection
        status, _ = self.request("/api/nodes/model", {"node_id": node["id"], "model": "qwen2.5-0.5b-instruct"},
                                 browser, base=self.nodes_url)
        self.assertEqual(status, 200)
        picked, output = read_until(process, r"Model: qwen2.5-0.5b-instruct")
        self.assertIsNotNone(picked, output)
        process.send_signal(signal.SIGINT)
        self.assertEqual(process.wait(timeout=5), 130)
        process.stdout.close()
        self.wait_for(lambda: self.node_row()["status"] == "offline")
        credentials = config_dir / "credentials.json"
        self.assertEqual(credentials.stat().st_mode & 0o777, 0o600)
        token = json.loads(credentials.read_text())["access_token"]
        status = subprocess.run(base + ["status", "--config-dir", str(config_dir)], capture_output=True, text=True, timeout=10)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn("Signed in as alice", status.stdout)
        switched = subprocess.run(base + ["status", "--server", "http://localhost:1234", "--config-dir", str(config_dir)], capture_output=True, text=True, timeout=10)
        self.assertNotEqual(switched.returncode, 0)
        logout = subprocess.run(base + ["logout", "--config-dir", str(config_dir)], capture_output=True, text=True, timeout=10)
        self.assertEqual(logout.returncode, 0, logout.stderr)
        self.assertFalse(credentials.exists())
        self.assertEqual(self.request("/auth/me", token=token)[0], 401)
        self.assertIsNone(self.node_row(), "Signing out removes the node")


class InstallerTests(unittest.TestCase):
    def test_installer_downloads_a_runnable_client_without_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sinferenced").mkdir()
            for module in CLIENT_MODULES:
                shutil.copyfile(CLIENT / f"{module}.py", root / f"sinferenced/{module}.py")

            class QuietHandler(SimpleHTTPRequestHandler):
                def log_message(self, *args):
                    pass

            server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=root))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                destination = root / "bin"
                env = dict(os.environ, SINFERENCE_SERVER="https://auth.example.invalid",
                           SINFERENCE_REPO_BASE=f"http://127.0.0.1:{server.server_port}",
                           SINFERENCE_INSTALL_DIR=str(destination), XDG_DATA_HOME=str(root / "share"))
                installed = subprocess.run(["sh", str(ROOT / "scripts/install.sh")], env=env,
                                           capture_output=True, text=True, timeout=10)
                self.assertEqual(installed.returncode, 0, installed.stderr)
                self.assertIn("https://auth.example.invalid", installed.stdout)
                client = root / "share/sinference/client"
                for module in CLIENT_MODULES:
                    self.assertEqual((client / f"{module}.py").read_bytes(), (CLIENT / f"{module}.py").read_bytes())
                self.assertEqual((client / "server").read_text().strip(), "https://auth.example.invalid")
                binary = destination / "sinference"
                self.assertEqual(binary.resolve(), (client / "cli.py").resolve())
                help_result = subprocess.run([str(binary), "--help"], capture_output=True, text=True, timeout=10)
                self.assertEqual(help_result.returncode, 0, help_result.stderr)
                self.assertIn("up,login,status,logout", help_result.stdout)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

if __name__ == "__main__":
    unittest.main()
