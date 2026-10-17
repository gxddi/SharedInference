import base64
import hashlib
import json
from pathlib import Path
import socket
import struct
import sys
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from test_auth import ROOT, Registry, ServiceTestCase, load

sys.path.insert(0, str(ROOT / "sinference/backend/orchestrator"))  # the orchestrator's own modules
import audits  # noqa: E402
from inference import Inference  # noqa: E402
from jobs import Jobs, split  # noqa: E402
from layouts import Layout  # noqa: E402
from openai_api import APIError, OpenAIAPI, request  # noqa: E402
from slices import Slices, first_gap, fit  # noqa: E402
from solders.keypair import Keypair  # noqa: E402
from stakes import STAKE, Stakes, discriminator  # noqa: E402

orchestrator = load("orchestrator_main", "sinference/backend/orchestrator/__main__.py")

GiB = 2**30
MiB = 2**20
# 4 layers of 1 GiB, embeddings + head of 0.5 GiB each
LAYOUT = Layout(layers=4, layer_bytes=[GiB] * 4, embed_bytes=GiB // 2, head_bytes=GiB // 2)
MODEL = "qwen2.5-7b-instruct"
PROGRAM = "Fg6PaFpoGXkYsidMpWTK6W2BeZ7FEfcYkg476zPFsLnS"
WALLET = "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"


class Chat:
    """Stand-in tokenizer: every prompt is [1, 2, 3], token t is the t-th letter, 0 ends a reply."""

    def prompt(self, repo, messages):
        return [1, 2, 3]

    def stop(self, repo):
        return {0}

    def text(self, repo, tokens):
        return "".join(chr(96 + token) for token in tokens if token)


class PlanningTests(unittest.TestCase):
    def test_first_gap_fills_holes_then_starts_a_new_copy(self):
        self.assertEqual(first_gap([], 4), (0, 0, 4))
        self.assertEqual(first_gap([(0, 0, 2)], 4), (0, 2, 4))
        self.assertEqual(first_gap([(0, 2, 4)], 4), (0, 0, 2))  # a hole at the front
        self.assertEqual(first_gap([(0, 0, 2), (0, 2, 4)], 4), (1, 0, 4))
        self.assertEqual(first_gap([(0, 0, 4), (1, 0, 1)], 4), (1, 1, 4))

    def test_fit_counts_embeddings_and_head(self):
        self.assertEqual(fit(LAYOUT, 0, 4, 2.5 * GiB), 2)  # 0.5 embeddings + 2 layers
        self.assertEqual(fit(LAYOUT, 2, 4, 2.5 * GiB), 4)  # 2 layers + 0.5 head
        self.assertEqual(fit(LAYOUT, 2, 4, 2.4 * GiB), 3)  # the head doesn't fit with layer 3
        self.assertEqual(fit(LAYOUT, 0, 4, 1.4 * GiB), 0)
        self.assertEqual(fit(LAYOUT, 1, 2, 10 * GiB), 2)   # never past the gap


class PayTests(unittest.TestCase):
    def test_nodes_are_paid_by_slice_size_and_speed(self):
        parts = [{"node_id": "a", "start_layer": 0, "end_layer": 8}, {"node_id": "b", "start_layer": 8, "end_layer": 12}]
        self.assertEqual(split(1200, parts, {"a": 2.0, "b": 1.0}), {"a": 800, "b": 400})  # as fast per layer: by size

        parts[1]["end_layer"] = 16
        self.assertEqual(split(1200, parts, {"a": 1.0, "b": 2.0}), {"a": 800, "b": 400})  # same size: by speed
        self.assertEqual(split(0, parts, {"a": 1.0, "b": 1.0}), {"a": 0, "b": 0})


class RequestTests(unittest.TestCase):
    def test_chat_requests_are_checked_and_normalized(self):
        model, messages, settings = request({"model": MODEL, "seed": 7, "max_tokens": 16, "messages": [
            {"role": "developer", "content": "Be brief."},
            {"role": "user", "content": [{"type": "text", "text": "Hi "}, {"type": "text", "text": "there"}]}]})
        self.assertEqual(messages, [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "Hi there"}])
        self.assertEqual(settings, {"max_tokens": 16, "temperature": 1.0, "top_p": 1.0, "seed": 7})

        for bad in ({"temperature": 3}, {"top_p": 0}, {"max_tokens": 0}, {"n": 2}, {"messages": []},
                    {"messages": [{"role": "robot", "content": "hi"}]}):
            with self.assertRaises(APIError):
                request({"model": MODEL, "messages": [{"role": "user", "content": "hi"}], **bad})


class StakeTests(unittest.TestCase):
    USER = "0123456789abcdef0123456789abcdef"

    def stakes(self, account, balance=0, sol_usd=100.0):
        """Stakes ($20 per node at sol_usd) against a stand-in RPC w/ one stake account (or none) and a wallet balance."""
        keypair = Path(self.enterContext(__import__("tempfile").TemporaryDirectory())) / "authority.json"
        keypair.write_text(Keypair().to_json())
        stakes, stakes.sent = Stakes("http://rpc.invalid", PROGRAM, keypair, 20, lambda: sol_usd), []

        def call(method, *params):
            if method == "getAccountInfo":
                return {"value": account}
            if method == "getBalance":
                return {"value": balance}
            return 1_000_000  # rent-exempt minimum
        stakes.call = call
        stakes.send = lambda instructions, signer=None: stakes.sent.append((instructions, signer)) or "signature"
        return stakes

    def account(self, lamports, banned=False):
        data = STAKE.pack(discriminator("account", "Stake"), bytes(32), bytes(16), banned, 255)
        return {"owner": PROGRAM, "lamports": lamports, "data": [base64.b64encode(data).decode(), "base64"]}

    def test_nodes_are_covered_from_the_accounts_wallet(self):
        wallet = Keypair()
        address, secret = str(wallet.pubkey()), str(wallet)
        self.assertIsNone(Stakes("", None, None, 20, None).cover(None, None, self.USER, 1))  # Solana off
        self.assertIn("Add a Solana wallet", self.stakes(None).cover(None, None, self.USER, 1))
        self.assertIn("SOL's price", self.stakes(None, sol_usd=None).cover(address, secret, self.USER, 1))
        self.assertIn("slashed", self.stakes(self.account(10**9, banned=True)).cover(address, secret, self.USER, 1))

        # $20 per node at $100 per SOL: 0.2 SOL each, already staked for 2 nodes
        covered = self.stakes(self.account(1_000_000 + 4 * 10**8))
        self.assertIsNone(covered.cover(address, secret, self.USER, 2))
        self.assertEqual(covered.sent, [])

        # Short: the rest comes from the wallet, signed by it
        short = self.stakes(self.account(1_000_000 + 10**8), balance=10**9)
        self.assertIsNone(short.cover(address, secret, self.USER, 2))
        [([instruction], signer)] = short.sent
        self.assertEqual(str(signer.pubkey()), address)
        self.assertEqual(bytes(instruction.data), discriminator("global", "stake") + bytes.fromhex(self.USER)
                         + struct.pack("<Q", 3 * 10**8))
        self.assertEqual(str(instruction.accounts[1].pubkey), str(short.address(address, self.USER)))

        # A wallet that can't cover it serves nothing
        poor = self.stakes(None, balance=10**6)
        self.assertIn(f"Your wallet {address} needs", poor.cover(address, secret, self.USER, 1))
        self.assertEqual(poor.sent, [])

    def test_a_payout_is_one_instruction_per_stake(self):
        stakes = self.stakes(None)
        job, record = "ab" * 16, '{"job":"x"}'
        stakes.pay(job, record, [{"wallet": WALLET, "user_id": "00" * 16, "lamports": 5},
                                 {"wallet": WALLET, "user_id": "00" * 16, "lamports": 7}])

        [(sent, _)] = stakes.sent
        self.assertEqual(len(sent), 1)  # the same owner's two nodes, paid together
        self.assertEqual(bytes(sent[0].data), discriminator("global", "pay") + bytes.fromhex(job)
                         + hashlib.sha256(record.encode()).digest() + struct.pack("<Q", 12))
        self.assertEqual(str(sent[0].accounts[2].pubkey), str(stakes.address(WALLET, "00" * 16)))

    def test_a_release_only_goes_to_the_stakes_own_wallet(self):
        stakes = self.stakes(None)
        stakes.release(WALLET, self.USER, 42)
        [([instruction], _)] = stakes.sent
        self.assertEqual(bytes(instruction.data), discriminator("global", "release") + struct.pack("<Q", 42))
        self.assertEqual([str(meta.pubkey) for meta in instruction.accounts[2:]],
                         [str(stakes.address(WALLET, self.USER)), WALLET])


class OrchestratorTests(ServiceTestCase):
    def setUp(self):
        super().setUp()
        self.start_node_service(link=True)
        audits.ROOT = Path(self.directory.name) / "audits"  # evidence stays out of data/

        # Orchestrator w/ a made-up layout, Solana off, linked like __main__.py would
        self.slices = Slices(self.database, lambda repo: LAYOUT)
        self.slices.setup()
        self.jobs = Jobs(self.database, lambda: 100.0)  # $100 per SOL
        self.jobs.setup()
        stakes = Stakes("", None, None, 20, None)
        self.audits = audits.Audits(self.database, stakes)
        self.audits.setup()
        self.orchestrator = orchestrator.Orchestrator(self.slices, self.jobs, self.audits, stakes)

        link = socket.create_connection(("127.0.0.1", self.link_port), timeout=5)
        self.addCleanup(link.close)
        threading.Thread(target=self.serve, args=(link,), daemon=True).start()
        self.browser = self.signup()

    def serve(self, link):
        try:
            self.orchestrator.serve(link)
        except OSError:
            pass  # test over

    def node(self, name, vram_free_mb=3584, vram_total_mb=8192, model=MODEL):
        """A connected node that picked a model and asked for a slice: (node id, connection)."""
        _, node = self.connect(self.node_token(self.browser), vram_total_mb, vram_free_mb, name)
        node_id = node.receive(5)["node_id"]
        self.request("/api/nodes/model", {"node_id": node_id, "model": model}, self.browser, base=self.nodes_url)
        self.assertEqual(node.receive(5)["type"], "model")
        node.send({"type": "join"})
        return node_id, node

    def status(self, node_id):
        with self.server.connect() as db:
            return db.execute("SELECT status FROM nodes WHERE id = %s", (node_id,)).fetchone()["status"]

    def held(self):
        with self.slices.connect() as db:
            return {(row["replica"], row["start_layer"], row["end_layer"], row["loaded"])
                    for row in db.execute("SELECT * FROM slices").fetchall()}

    def test_slices_fill_linearly_and_reopen(self):
        # 3.5 GB free - 1 GB headroom = 2.5 GB each
        a, node_a = self.node("a")
        slice_a = node_a.receive(5)
        self.assertEqual((slice_a["type"], slice_a["replica"], slice_a["start_layer"], slice_a["end_layer"]),
                         ("slice", 0, 0, 2))
        self.assertIn("transformers==5.20.1", slice_a["packages"])

        b, node_b = self.node("b")
        self.assertEqual(node_b.receive(5)["start_layer"], 2)

        # Loaded → ready
        node_a.send({"type": "ready", "model": MODEL, "start_layer": 0, "end_layer": 2})
        self.wait_for(lambda: self.status(a) == "ready")
        self.wait_for(lambda: (0, 0, 2, True) in self.held())

        # Copy 0 is full, so the next node starts copy 1
        c, node_c = self.node("c")
        self.assertEqual((node_c.receive(5)["replica"]), 1)

        # Ready → online gives the slice up, and the next node gets it
        node_a.send({"type": "unloaded"})
        self.wait_for(lambda: self.status(a) == "online" and (0, 0, 2, True) not in self.held())
        _, node_d = self.node("d")
        self.assertEqual((lambda s: (s["replica"], s["start_layer"]))(node_d.receive(5)), (0, 0))

        # Going offline gives it up too
        node_b.sock.close()
        self.wait_for(lambda: (0, 2, 4, False) not in self.held())

    def test_cpu_and_small_nodes_get_no_slice(self):
        _, cpu = self.node("cpu", vram_free_mb=None, vram_total_mb=None)
        self.assertEqual(cpu.receive(5)["type"], "no_slice")
        _, small = self.node("small", vram_free_mb=1100)
        self.assertIn("Not enough free VRAM", small.receive(5)["message"])
        self.assertEqual(self.held(), set())


    #############
    # Inference #
    #############

    def serving(self):
        """Two ready nodes holding a whole copy, answering steps like workers: (node ids, hidden payloads sent)."""
        ids, sent = [], []
        for name, (start, end) in (("a", (0, 2)), ("b", (2, 4))):
            node_id, node = self.node(name)
            self.assertEqual((node.receive(5)["start_layer"]), start)
            node.send({"type": "ready", "model": MODEL, "start_layer": start, "end_layer": end})
            threading.Thread(target=self.work, args=(node, start == 0, sent), daemon=True).start()
            ids.append(node_id)
        self.wait_for(lambda: self.slices.replica(MODEL) is not None)
        return ids, sent

    def work(self, node, first, sent):
        """Stand-in worker: the first slice sends 2-wide hidden states, the last spells "hi" then stops."""
        replies = {}
        try:
            while True:
                received = node.receive(30, payload=True)
                if not received or received[0]["type"] != "forward":
                    continue
                message, payload = received
                if first:
                    hidden = bytes([len(message["tokens"])]) * (len(message["tokens"]) * 4)
                    sent.append(hidden)
                    node.send({"type": "hidden", "job": message["job"], "shape": [len(message["tokens"]), 2]}, hidden)
                else:
                    self.assertEqual(len(payload), message["shape"][0] * 4)
                    token = next(replies.setdefault(message["job"], iter([8, 9, 0])))
                    node.send({"type": "token", "job": message["job"], "token": token})
        except (OSError, ValueError):
            pass  # test over

    def start_api(self):
        api = OpenAIAPI(("127.0.0.1", 0), self.database,
                        Inference(self.slices, self.orchestrator.pipeline, self.jobs, Chat()), self.slices)
        threading.Thread(target=api.serve_forever, daemon=True).start()
        self.addCleanup(api.server_close)
        self.addCleanup(api.shutdown)
        self.api_url = f"http://127.0.0.1:{api.server_port}"
        return self.request("/auth/keys", {"name": "tests"}, self.browser)[1]["key"]

    def call(self, key, path="/v1/chat/completions", **body):
        data = json.dumps({"model": MODEL, "messages": [{"role": "user", "content": "hi"}], **body}).encode()
        request = Request(self.api_url + path, data if path.endswith("completions") else None,
                          {"Content-Type": "application/json", "Authorization": "Bearer " + key})
        try:
            response = urlopen(request, timeout=10)
        except HTTPError as exc:
            response = exc
        with response:
            return response.code, response.read()

    def job(self, job_id):
        with self.jobs.connect() as db:
            return db.execute("SELECT * FROM jobs WHERE id = %s", (job_id,)).fetchone()

    def test_a_chat_runs_across_slices_and_is_recorded(self):
        key = self.start_api()
        self.assertEqual(self.call("wrong", "/v1/models")[0], 401)
        self.assertEqual(json.loads(self.call(key, "/v1/models")[1])["data"], [])  # nothing serving yet
        self.assertEqual(self.call(key)[0], 503)

        _, sent = self.serving()
        self.assertEqual([model["id"] for model in json.loads(self.call(key, "/v1/models")[1])["data"]], [MODEL])

        # Whole reply
        status, body = self.call(key)
        reply = json.loads(body)
        self.assertEqual(status, 200, reply)
        self.assertEqual(reply["choices"][0], {"index": 0, "message": {"role": "assistant", "content": "hi"},
                                               "finish_reason": "stop"})
        self.assertEqual(reply["usage"], {"prompt_tokens": 3, "completion_tokens": 3, "total_tokens": 6})

        # On record: 3 prompt tokens at $0.10/M + 3 completion at $0.20/M = $0.0000009 = 9 lamports at $100/SOL,
        # split between the nodes, and every hidden state crossing the boundary hashed
        job = self.job(reply["id"].removeprefix("chatcmpl-"))
        record = json.loads(job["record"])
        self.assertEqual(job["status"], "done")
        self.assertEqual(record["lamports"], 9)
        self.assertLessEqual(sum(part["lamports"] for part in record["parts"]), 9)
        self.assertTrue(all(part["seconds"] > 0 for part in record["parts"]))
        self.assertEqual(record["boundaries"], [hashlib.sha256(b"".join(sent)).hexdigest()])

        # As it's generated
        status, body = self.call(key, stream=True, stream_options={"include_usage": True})
        events = [line.removeprefix(b"data: ") for line in body.split(b"\n\n") if line]
        self.assertEqual(events[-1], b"[DONE]")
        chunks = [json.loads(event) for event in events[:-1]]
        self.assertEqual("".join(chunk["choices"][0]["delta"].get("content", "") for chunk in chunks if chunk["choices"]), "hi")
        self.assertEqual(chunks[-2]["choices"][0]["finish_reason"], "stop")
        self.assertEqual(chunks[-1]["usage"]["total_tokens"], 6)

    def test_sol_goes_back_once_audits_are_past(self):
        key = self.start_api()
        with self.server.connect() as db:
            user_id = db.execute("SELECT id FROM users WHERE username = 'alice'").fetchone()["id"]
        Registry(self.database).set_wallet(user_id, WALLET, "key")
        self.serving()

        # Earnings: once the job's audit window has passed, and once
        job_id = json.loads(self.call(key)[1])["id"].removeprefix("chatcmpl-")
        with self.jobs.connect() as db:
            db.execute("UPDATE jobs SET payout = 'paid' WHERE id = %s", (job_id,))
            earned = db.execute("SELECT sum(lamports)::bigint AS n FROM job_parts WHERE job_id = %s", (job_id,)).fetchone()["n"]
        later = time.time() + 1
        self.assertEqual(self.jobs.earnings(time.time() - 600), [])
        self.assertEqual(self.jobs.earnings(later), [{"wallet": WALLET, "user_id": user_id, "lamports": earned}])
        self.jobs.released(WALLET, user_id, later, "released")
        self.assertEqual(self.jobs.earnings(later), [])

        # Collateral: once the account's nodes have all left
        self.jobs.staking(WALLET, user_id)
        self.assertEqual(self.jobs.idle(later), [])
        with self.jobs.connect() as db:
            db.execute("UPDATE nodes SET status = 'offline', last_seen = 0")
        self.assertEqual(self.jobs.idle(later), [{"wallet": WALLET, "user_id": user_id}])
        self.jobs.emptied(WALLET, user_id, "emptied")
        self.assertEqual(self.jobs.idle(later), [])

    def test_audited_jobs_keep_evidence_and_fraud_bans_the_owner(self):
        key = self.start_api()
        nodes, sent = self.serving()

        rate, audits.RATE = audits.RATE, 1  # audit everything
        self.addCleanup(setattr, audits, "RATE", rate)
        job_id = json.loads(self.call(key)[1])["id"].removeprefix("chatcmpl-")

        # The evidence: what crossed the boundary, matching the record's hashes
        folder = audits.ROOT / job_id
        manifest = json.loads((folder / "manifest.json").read_text())
        self.assertEqual((folder / "boundary-0.bin").read_bytes(), b"".join(sent))
        self.assertEqual(manifest["boundaries"], json.loads(self.job(job_id)["record"])["boundaries"])
        self.assertEqual((manifest["prompt"], manifest["output"]), ([1, 2, 3], [8, 9, 0]))

        # Fraud: the owner is banned, their slices are gone, and they can't get new ones
        self.assertIsNone(self.audits.fraud(job_id, nodes[1]))  # Solana off: nothing to slash
        self.assertTrue(self.audits.banned(self.job(job_id)["user_id"], None))
        self.assertIsNone(self.slices.replica(MODEL))
        self.assertEqual(self.call(key)[0], 503)
        _, node = self.node("c")
        self.assertIn("banned", node.receive(5)["message"])

        with self.assertRaises(ValueError):
            self.audits.fraud("00" * 16, nodes[1])  # never audited


if __name__ == "__main__":
    unittest.main()
