import json
from pathlib import Path
import tempfile
import time
import unittest

from solders.keypair import Keypair

from test_auth import ServiceTestCase
import hardware


class HardwareTests(unittest.TestCase):
    def card(self, root, name, vendor, windows):
        """A stand-in /sys/class/drm/<name> for a display controller w/ these PCI memory windows (bytes)."""
        device = root / name / "device"
        device.mkdir(parents=True)
        (device / "vendor").write_text(vendor + "\n")
        (device / "class").write_text("0x030000\n")
        (device / "resource").write_text("".join(f"0x{start:016x} 0x{start + size - 1:016x} 0x0000000000140204\n"
                                                 for start, size in windows) + "0x0 0x0 0x0\n")

    def test_intel_gpus_count_by_their_largest_window(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        drm, hardware.DRM = hardware.DRM, root
        self.addCleanup(setattr, hardware, "DRM", drm)

        self.card(root, "card0", "0x8086", [(0xe0000000, 16 * 2**20), (0xd0000000, 256 * 2**20)])  # integrated
        (root / "card0-eDP-1").mkdir()                                                          # a connector
        self.assertIsNone(hardware.intel())

        self.card(root, "card1", "0x8086", [(0x4000000000, 16 * 2**30), (0xa0000000, 16 * 2**20)])  # Arc, w/ ReBAR
        self.card(root, "card2", "0x10de", [(0x5000000000, 24 * 2**30)])                           # not Intel
        self.assertEqual(hardware.intel(), 16 * 1024)


class NodeServiceTests(ServiceTestCase):
    def setUp(self):
        super().setUp()
        self.port = self.start_node_service()

    def config(self, path, data=None, token=None):
        return self.request("/api/nodes" + path, data, token, base=self.nodes_url)

    def test_connection_maintains_live_node_row(self):
        browser = self.signup()
        sock, node = self.connect(self.node_token(browser))

        # Hello registers the node
        welcome = node.receive(5)
        self.assertEqual((welcome["type"], welcome["model"]), ("welcome", None))
        row = self.node_row()
        self.assertEqual((row["id"], row["name"], row["ip"], row["status"]), (welcome["node_id"], "gpu-box", "127.0.0.1", "online"))
        self.assertEqual((row["vram_total_mb"], row["vram_free_mb"]), (8192, 6144))

        # Heartbeats keep it current
        node.send({"type": "heartbeat", "vram_free_mb": 4096})
        self.wait_for(lambda: self.node_row()["vram_free_mb"] == 4096)
        _, listed = self.config("/list", token=browser)
        self.assertEqual([n["id"] for n in listed["nodes"]], [welcome["node_id"]])
        self.assertIn("qwen2.5-7b-instruct", [model["id"] for model in listed["models"]])
        self.assertEqual(self.config("/list", token=self.signup("bob"))[1]["nodes"], [])

        # A browser pick is pushed right away, no heartbeat needed
        status, _ = self.config("/model", {"node_id": welcome["node_id"], "model": "qwen2.5-7b-instruct"}, browser)
        self.assertEqual(status, 200)
        self.assertEqual(node.receive(2), {"type": "model", "node_id": welcome["node_id"], "model": "qwen2.5-7b-instruct"})

        # Leaving marks it offline
        sock.close()
        self.wait_for(lambda: self.node_row()["status"] == "offline")

    def test_reconnect_reuses_row_and_tracks_newest_connection(self):
        token = self.node_token(self.signup())
        _, first_node = self.connect(token)
        node_id = first_node.receive(5)["node_id"]
        _, second_node = self.connect(token, vram_total_mb=None, vram_free_mb=None)
        self.assertEqual(second_node.receive(5)["node_id"], node_id)
        self.assertIsNone(self.node_row()["vram_total_mb"])

        with self.assertRaises(ConnectionError):
            first_node.receive(5)  # the service closes the replaced connection
        time.sleep(.2)             # let its handler finish
        self.assertEqual(self.node_row()["status"], "online", "Closing a replaced connection must not mark the node offline")

    def test_rejects_invalid_and_revoked_sessions(self):
        browser = self.signup()
        for token in ("not-a-token", browser):
            _, node = self.connect(token)
            self.assertEqual(node.receive(5)["type"], "error")

        token = self.node_token(browser)
        _, node = self.connect(token)
        node.receive(5)
        self.assertEqual(self.request("/auth/logout", {}, token)[0], 200)
        self.assertIsNone(self.node_row(), "Revoking the session removes the node")
        node.send({"type": "heartbeat", "vram_free_mb": 1})
        self.assertEqual(node.receive(5)["type"], "error")

    def test_an_account_adds_one_wallet_from_the_browser_or_a_node(self):
        alice = self.signup()
        node_token = self.node_token(alice)
        self.assertEqual(self.config("/wallet", token=alice), (200, {"needed": False, "address": None}))  # Solana off

        wallet = Keypair()
        self.assertEqual(self.config("/wallet", {"key": "not a key"}, node_token)[0], 400)
        self.assertEqual(self.config("/wallet", {"key": json.dumps(list(bytes(wallet)))}, node_token),
                         (200, {"address": str(wallet.pubkey())}))
        self.assertEqual(self.config("/wallet", token=alice)[1]["address"], str(wallet.pubkey()))

        replaced = Keypair()  # the browser can replace it, by base58
        self.assertEqual(self.config("/wallet", {"key": str(replaced)}, alice)[1]["address"], str(replaced.pubkey()))
        self.assertEqual(self.config("/list", token=alice)[1]["wallet"], str(replaced.pubkey()))

    def test_only_the_owner_can_choose_a_valid_model(self):
        alice = self.signup()
        token = self.node_token(alice)
        _, node = self.connect(token)
        node_id = node.receive(5)["node_id"]

        self.assertEqual(self.config("/model", {"node_id": node_id, "model": "gpt-5"}, alice)[0], 400)
        self.assertEqual(self.config("/model", {"node_id": node_id, "model": "qwen2.5-0.5b-instruct"}, self.signup("bob"))[0], 404)
        self.assertEqual(self.config("/list", token=token)[0], 401)  # node sessions can't use the browser API
        self.assertEqual(self.config("/model", {"node_id": node_id, "model": "qwen2.5-0.5b-instruct"}, alice)[0], 200)
        self.assertEqual(self.request("/api/nodes/model", {"node_id": node_id, "model": "qwen2.5-0.5b-instruct"}, alice,
                                      origin="https://other.example", base=self.nodes_url)[0], 403)


if __name__ == "__main__":
    unittest.main()
