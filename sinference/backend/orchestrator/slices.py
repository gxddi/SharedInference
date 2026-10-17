"""Which node holds which layers of which model, and handing out new slices (linearly, for now)."""

import random

import psycopg
from psycopg.rows import dict_row

HEADROOM = 1024 * 2**20  # bytes kept free on each GPU for activations + KV cache

# What nodes install to load + run slices. Sent w/ every slice, so every node matches.
PACKAGES = ["torch==2.14.1", "transformers==5.20.1", "safetensors==0.8.0", "huggingface_hub==2.2.0"]

# Needs the nodes backend's nodes, models + wallets tables. One slice per node.
SCHEMA = """
CREATE TABLE IF NOT EXISTS slices (
    node_id TEXT PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
    model TEXT NOT NULL REFERENCES models(id) ON DELETE CASCADE,
    replica INTEGER NOT NULL, start_layer INTEGER NOT NULL, end_layer INTEGER NOT NULL,
    loaded BOOLEAN NOT NULL DEFAULT false
);
"""


def first_gap(taken, layers):
    """(replica, start, stop) of the first range of layers nobody holds.

    taken: (replica, start_layer, end_layer) of every held slice of the model.
    """
    replica = 0
    while True:
        cursor = 0
        for start, end in sorted((s, e) for r, s, e in taken if r == replica):
            if start > cursor:
                return replica, cursor, start
            cursor = max(cursor, end)
        if cursor < layers:
            return replica, cursor, layers
        replica += 1  # this copy is full, start the next one


def fit(layout, start, stop, budget):
    """End of the longest run of layers from start (up to stop) that fits in budget bytes."""
    used = layout.embed_bytes if start == 0 else 0
    end = start
    while end < stop:
        need = layout.layer_bytes[end] + (layout.head_bytes if end + 1 == layout.layers else 0)
        if used + need > budget:
            break
        used += need
        end += 1
    return end


class Slices:
    def __init__(self, database_url, layout):
        self.database_url = database_url
        self.layout = layout  # repo → Layout (layouts.from_hub, or a stand-in in tests)
        self.layouts = {}

    def connect(self):
        return psycopg.connect(self.database_url, row_factory=dict_row, connect_timeout=10)

    def setup(self):
        with self.connect() as db:
            db.execute(SCHEMA)

    def layout_of(self, repo):
        if repo not in self.layouts:
            self.layouts[repo] = self.layout(repo)
        return self.layouts[repo]


    ###########
    # Changes #
    ###########

    def assign(self, node_id):
        """Give a node the first open slice of its model. (slice message, None) or (None, why not)."""
        with self.connect() as db:
            node = db.execute("""SELECT nodes.model, nodes.vram_free_mb, nodes.status, models.repo
                FROM nodes JOIN models ON models.id = nodes.model WHERE nodes.id = %s""", (node_id,)).fetchone()
        if not node or node["status"] == "offline":
            return None, "Choose a model for this node first."
        if node["vram_free_mb"] is None:
            return None, "No GPU detected. Only GPU nodes can serve model slices."
        layout = self.layout_of(node["repo"])  # may hit Hugging Face, so outside the transaction

        with self.connect() as db:
            db.execute("SELECT pg_advisory_xact_lock(hashtext('slices'))")  # one assignment at a time
            db.execute("DELETE FROM slices WHERE node_id = %s", (node_id,))  # rejoining gives up the old one

            # Slices of nodes that are still around
            taken = db.execute("""SELECT replica, start_layer, end_layer FROM slices
                JOIN nodes ON nodes.id = slices.node_id
                WHERE slices.model = %s AND nodes.status <> 'offline'""", (node["model"],)).fetchall()
            replica, start, stop = first_gap([(t["replica"], t["start_layer"], t["end_layer"]) for t in taken],
                                             layout.layers)
            end = fit(layout, start, stop, node["vram_free_mb"] * 2**20 - HEADROOM)
            if end == start:
                return None, "Not enough free VRAM for a layer of this model. Try a smaller one."

            db.execute("INSERT INTO slices VALUES (%s, %s, %s, %s, %s)", (node_id, node["model"], replica, start, end))

        return {"type": "slice", "model": node["model"], "repo": node["repo"], "replica": replica,
                "start_layer": start, "end_layer": end, "layers": layout.layers, "packages": PACKAGES}, None

    def loaded(self, node_id, model, start, end):
        """The node finished loading. False if that isn't the slice it was given."""
        with self.connect() as db:
            return db.execute("""UPDATE slices SET loaded = true WHERE node_id = %s AND model = %s
                AND start_layer = %s AND end_layer = %s RETURNING node_id""",
                              (node_id, model, start, end)).fetchone() is not None

    def release(self, node_id):
        """The node went offline or dropped its slice: the layers are open again."""
        with self.connect() as db:
            db.execute("DELETE FROM slices WHERE node_id = %s", (node_id,))

    def tidy(self):
        """After a restart: drop stale slices, and list connected nodes still waiting for one."""
        with self.connect() as db:
            db.execute("""DELETE FROM slices USING nodes WHERE nodes.id = slices.node_id
                AND (nodes.status = 'offline' OR (slices.loaded AND nodes.status = 'online'))""")
            return [row["id"] for row in db.execute("""SELECT id FROM nodes WHERE status = 'online'
                AND model IS NOT NULL AND id NOT IN (SELECT node_id FROM slices)""").fetchall()]


    ###########
    # Serving #
    ###########

    def repo(self, model):
        with self.connect() as db:
            row = db.execute("SELECT repo FROM models WHERE id = %s", (model,)).fetchone()
        return row and row["repo"]

    def owner(self, node_id):
        """A node's owner, their wallet (+ its key), and how many of their other nodes hold slices."""
        with self.connect() as db:
            return db.execute("""SELECT nodes.user_id, wallets.address AS wallet, wallets.secret,
                (SELECT count(*) FROM slices JOIN nodes AS theirs ON theirs.id = slices.node_id
                 WHERE theirs.user_id = nodes.user_id AND theirs.id <> nodes.id) AS others
                FROM nodes LEFT JOIN wallets ON wallets.user_id = nodes.user_id
                WHERE nodes.id = %s""", (node_id,)).fetchone()

    def replica(self, model):
        """A whole, ready copy of a model, its slices in layer order (picked at random), or None.

        [{node_id, start_layer, end_layer, user_id, wallet}]
        """
        repo = self.repo(model)
        if not repo:
            return None
        layers = self.layout_of(repo).layers

        with self.connect() as db:
            rows = db.execute("""SELECT slices.replica, slices.node_id, slices.start_layer, slices.end_layer,
                nodes.user_id, wallets.address AS wallet
                FROM slices JOIN nodes ON nodes.id = slices.node_id LEFT JOIN wallets ON wallets.user_id = nodes.user_id
                WHERE slices.model = %s AND slices.loaded AND nodes.status = 'ready'
                ORDER BY slices.replica, slices.start_layer""", (model,)).fetchall()

        # Copies whose slices cover every layer, end to end
        copies = {}
        for row in rows:
            copies.setdefault(row.pop("replica"), []).append(row)
        whole = [parts for parts in copies.values()
                 if parts[0]["start_layer"] == 0 and parts[-1]["end_layer"] == layers
                 and all(a["end_layer"] == b["start_layer"] for a, b in zip(parts, parts[1:]))]
        return random.choice(whole) if whole else None

    def available(self):
        """Models w/ a whole, ready copy: [{id, name}]"""
        with self.connect() as db:
            models = db.execute("""SELECT DISTINCT models.id, models.name, models.memory_gb FROM models
                JOIN slices ON slices.model = models.id WHERE slices.loaded ORDER BY models.memory_gb""").fetchall()
        return [{"id": model["id"], "name": model["name"]} for model in models if self.replica(model["id"])]
