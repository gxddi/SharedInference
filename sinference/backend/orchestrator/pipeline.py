"""
Runs a job across the nodes holding a copy of a model, one token at a time.

Every step passes through here (nodes can't reach each other): token ids go to
the first slice, each slice's hidden states on to the next, and the last slice
sends back the next token. See docs/inference.md.
"""

import queue
import threading
import time

STEP_TIMEOUT = 120  # seconds a node gets for one step (a long prompt on a slow GPU)


class JobError(Exception):
    pass


class Pipeline:
    def __init__(self, link):
        self.link = link
        self.replies = {}  # (node_id, job) → where its reply goes
        self.busy = {}     # node_id → lock, so a node runs one step at a time
        self.lock = threading.Lock()


    ###########
    # Replies #
    ###########

    def reply(self, message, payload):
        """A node's hidden / token / error, from the link's thread."""
        waiting = self.replies.get((message["node_id"], message["job"]))
        if waiting:
            waiting.put((message, payload))

    def lost(self, node_id):
        """A node went offline or dropped its slice: its steps in flight fail."""
        with self.lock:
            stuck = [waiting for (node, _), waiting in self.replies.items() if node == node_id]
        for waiting in stuck:
            waiting.put(({"type": "error", "message": "it left"}, b""))


    ########
    # Jobs #
    ########

    def step(self, node_id, message, payload=b""):
        """Run one step on a node; its reply."""
        with self.lock:
            busy = self.busy.setdefault(node_id, threading.Lock())

        key, waiting = (node_id, message["job"]), queue.Queue()
        with busy:
            with self.lock:
                self.replies[key] = waiting
            try:
                if not self.link.send(node_id, message, payload):
                    raise JobError("Lost the nodes backend.")
                reply, payload = waiting.get(timeout=STEP_TIMEOUT)
            except queue.Empty:
                raise JobError("A node took too long.") from None
            finally:
                with self.lock:
                    del self.replies[key]

        if reply["type"] == "error":
            raise JobError(f"A node failed ({reply['message']}).")
        return reply, payload

    def run(self, job, nodes, prompt, settings, stop, trace, seconds):
        """Generate a job's tokens; yields each new token id.

        nodes: the copy's nodes in layer order
        stop: token ids that end it (end of sequence)
        trace: audits.Trace, for what crosses each boundary
        seconds: filled in w/ each node's time on its steps, round trips included
        """
        sample = {key: settings[key] for key in ("temperature", "top_p", "seed")}
        tokens = prompt
        try:
            for _ in range(settings["max_tokens"]):
                message, payload = {"type": "forward", "job": job, "tokens": tokens}, b""

                for index, node_id in enumerate(nodes):
                    last = index == len(nodes) - 1
                    if last:
                        message["sample"] = sample

                    started = time.monotonic()
                    reply, payload = self.step(node_id, message, payload)
                    seconds[node_id] = seconds.get(node_id, 0) + time.monotonic() - started
                    if reply["type"] != ("token" if last else "hidden"):
                        raise JobError(f"A node sent {reply['type']} instead of {'a token' if last else 'hidden states'}.")
                    if not last:
                        trace.add(index, payload)
                        message = {"type": "forward", "job": job, "shape": reply["shape"]}

                yield reply["token"]
                if reply["token"] in stop:
                    return
                tokens = [reply["token"]]

        finally:
            for node_id in nodes:
                self.link.send(node_id, {"type": "end", "job": job})  # free its KV cache
