"""
Runs the model worker (worker.py) as a child process, and relays jobs to and from it.
"""

import os
from pathlib import Path
import subprocess
import sys

from frames import Reader, pack

WORKER = Path(__file__).resolve().parent / "worker.py"


def same(a, b):
    keys = ("model", "start_layer", "end_layer")
    return a is not None and b is not None and all(a[key] == b[key] for key in keys)


class Worker:
    def __init__(self):
        self.process = None
        self.slice = None    # the slice message it's loading / holding
        self.state = None    # loading, ready, or failed
        self.error = None
        self.reader = Reader()

    def holds(self, slice):
        return self.state == "ready" and same(self.slice, slice)

    def fileno(self):
        """What to select() on for its output, or None w/o a worker."""
        return self.process.stdout.fileno() if self.process else None

    def start(self, slice, device):
        """Load a new slice on device (cuda, xpu), dropping whatever was held."""
        self.stop()
        self.slice, self.state, self.error, self.reader = slice, "loading", None, Reader()

        # Progress goes straight to the terminal (stderr); messages come back on stdout
        packages = [argument for package in slice["packages"] for argument in ("--package", package)]
        self.process = subprocess.Popen(
            [sys.executable, str(WORKER), "--repo", slice["repo"],
             "--start", str(slice["start_layer"]), "--end", str(slice["end_layer"]), *packages,
             *(["--device", device] if device else [])],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        os.set_blocking(self.process.stdout.fileno(), False)

    def send(self, message, payload=b""):
        """Relay a job message. False if nothing is held to run it."""
        if self.state != "ready":
            return False
        try:
            self.process.stdin.write(pack(message, payload))
            self.process.stdin.flush()
        except OSError:
            return False  # it died; poll() notices
        return True

    def poll(self):
        """Everything the worker said since last time, w/o waiting: [(message, payload)].

        Loading ends w/ a ready or failed message; dying at any point counts as failed.
        """
        if self.state not in ("loading", "ready"):
            return []

        # Whatever it has written so far
        try:
            while data := os.read(self.fileno(), 2**20):
                self.reader.feed(data)
        except BlockingIOError:
            pass
        try:
            received = self.reader.messages()
        except ValueError:  # not a message: whatever it is, the worker's broken
            self.state, self.error = "failed", "the worker wrote something that isn't a message"
            return [({"type": "failed", "message": self.error}, b"")]

        for message, _ in received:
            if message.get("type") in ("ready", "failed"):
                self.state, self.error = message["type"], message.get("message")

        # Died (crash, killed, out of memory)
        if self.state in ("loading", "ready") and self.process.poll() is not None:
            self.state, self.error = "failed", f"worker exited ({self.process.returncode})"
            received.append(({"type": "failed", "message": self.error}, b""))
        return received

    def stop(self):
        """Free the slice. True if one was loading or held."""
        if not self.process:
            return False
        try:
            self.process.stdin.close()  # the worker holds its slice until stdin closes
        except OSError:
            pass
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.process, self.slice, self.state = None, None, None
        return True
