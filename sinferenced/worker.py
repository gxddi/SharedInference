#!/usr/bin/env python3
"""
Loads one slice of a model onto the GPU, then runs it for jobs until stdin closes.

    worker.py --repo Qwen/Qwen2.5-7B-Instruct --start 0 --end 14 --package torch==2.14.1 ...

Runs in its own environment w/ the packages the orchestrator sent (PyTorch etc.),
set up on first use and whenever they change. Talks to the CLI in frames.py
messages; progress goes to stderr.

  worker → CLI   {"type": "ready"} or {"type": "failed", "message"}, once loaded
                 {"type": "hidden", "job", "shape"} + bf16 bytes   (not the last slice)
                 {"type": "token", "job", "token"}                 (the last slice)
                 {"type": "error", "job", "message"}
  CLI → worker   {"type": "forward", "job", "tokens"}              (the first slice)
                 {"type": "forward", "job", "shape"} + bf16 bytes  (the others)
                 the last slice's forwards also carry "sample": {temperature, top_p, seed}
                 {"type": "end", "job"}                            frees the job's KV cache
"""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import frames

DATA = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "sinference"
VENV = DATA / "venv"
MODELS = DATA / "models"  # Hugging Face downloads (only the shards a slice needs)
PACKAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*==[A-Za-z0-9.+!_-]+")  # name==version, nothing else
MAX_JOBS = 16  # KV caches held at once; the oldest goes first
TORCH_XPU = "https://download.pytorch.org/whl/xpu"  # PyTorch built for Intel GPUs

channel = sys.stdout.buffer  # → the CLI


def say(message, payload=b""):
    channel.write(frames.pack(message, payload))
    channel.flush()


def log(text):
    print(text, file=sys.stderr, flush=True)  # → the terminal


###############
# Environment #
###############

def ensure_environment(packages, device, argv):
    """Install the orchestrator's packages (for this device) into the worker's venv if they changed,
    then re-run inside it."""
    marker, wanted = VENV / "packages.txt", "\n".join([*packages, f"for {device}"])  # what's installed now
    if not (marker.exists() and marker.read_text() == wanted):
        log("Setting up the model worker (downloads PyTorch etc., a few GB the first time)…")
        try:  # w/ its output in the terminal: stdout is the CLI's
            subprocess.run([sys.executable, "-m", "venv", "--clear", str(VENV)], check=True, stdout=sys.stderr)
        except subprocess.CalledProcessError:
            raise OSError("Python can't make environments here (on Debian/Ubuntu: sudo apt install python3-venv)") from None
        pip = [str(VENV / "bin/python"), "-m", "pip", "install", "--progress-bar", "on"]  # big downloads: show them

        # Intel GPUs: PyTorch from Intel's builds first, then everything else as usual
        if device == "xpu":
            subprocess.run([*pip, "--index-url", TORCH_XPU, *(p for p in packages if p.startswith("torch=="))],
                           check=True, stdout=sys.stderr)
        subprocess.run([*pip, *packages], check=True, stdout=sys.stderr)
        marker.write_text(wanted)

    os.environ["SINFERENCE_WORKER_ENV"] = "1"  # inside it now, don't set up again
    os.execv(VENV / "bin/python", [str(VENV / "bin/python"), __file__, *argv])


#########
# Slice #
#########

def load_slice(repo, start, end, device):
    import torch
    from huggingface_hub import hf_hub_download
    from huggingface_hub.errors import EntryNotFoundError
    from safetensors import safe_open
    from transformers import AutoConfig, AutoModelForCausalLM

    config = AutoConfig.from_pretrained(repo, cache_dir=MODELS)
    layers = config.num_hidden_layers
    first, last = start == 0, end == layers
    tied = config.tie_word_embeddings
    if not 0 <= start < end <= layers:
        raise ValueError(f"layers {start}–{end} don't fit a {layers}-layer model")

    def wanted(name):
        if name.startswith("model.layers."):
            return start <= int(name.split(".")[2]) < end
        if name.startswith("model.embed_tokens."):
            return first or (last and tied)  # tied: the head reuses the embeddings
        return last  # model.norm, lm_head

    def local(name):
        """model.layers.17.x → model.layers.3.x, when the slice starts at 14"""
        if not name.startswith("model.layers."):
            return name
        _, _, index, rest = name.split(".", 3)
        return f"model.layers.{int(index) - start}.{rest}"

    # Which files hold our tensors (small models are one file, big ones are sharded)
    try:
        index = json.loads(Path(hf_hub_download(repo, "model.safetensors.index.json", cache_dir=MODELS)).read_text())
        files = sorted({file for name, file in index["weight_map"].items() if wanted(name)})
    except EntryNotFoundError:
        files = ["model.safetensors"]

    # Skeleton on the meta device: shapes only, no memory
    with torch.device("meta"):
        model = AutoModelForCausalLM.from_config(config, dtype=torch.bfloat16)
    body = model.model
    body.layers = torch.nn.ModuleList(body.layers[start:end])
    for i, block in enumerate(body.layers):
        block.self_attn.layer_idx = i  # caches are per slice
    if not (first or (last and tied)):
        body.embed_tokens = None
    if not last:
        body.norm = torch.nn.Identity()  # hidden states leave as they are
        model.lm_head = None
    with device:
        body.rotary_emb = type(body.rotary_emb)(config)  # computed, not stored

    # Fill in only our tensors, one at a time
    for file in files:
        log(f"Loading {file}…")
        path = hf_hub_download(repo, file, cache_dir=MODELS)
        with safe_open(path, framework="pt", device=str(device) if device.type == "cuda" else "cpu") as weights:
            for name in weights.keys():
                if wanted(name):
                    tensor = weights.get_tensor(name).to(device, torch.bfloat16)  # via the CPU, except on CUDA
                    module, _, attribute = local(name).rpartition(".")
                    model.get_submodule(module)._parameters[attribute] = torch.nn.Parameter(tensor, requires_grad=False)

    if last and tied:
        model.lm_head.weight = body.embed_tokens.weight

    missing = [name for name, parameter in model.named_parameters() if parameter.is_meta]
    if missing:
        raise ValueError(f"missing weights: {missing[:3]}")
    return model.eval()


#########
# Serve #
#########

class Slice:
    """A loaded slice, running jobs a step at a time w/ a KV cache each."""

    def __init__(self, model, first, last, device):
        self.model, self.first, self.last, self.device = model, first, last, device
        self.caches = {}      # job → KV cache
        self.generators = {}  # job → seeded sampler (last slice only)

    def forward(self, message, payload):
        import torch
        from transformers import DynamicCache

        job = message["job"]
        if job not in self.caches:
            if len(self.caches) >= MAX_JOBS:
                self.end(next(iter(self.caches)))
            self.caches[job] = DynamicCache(config=self.model.config)

        # In: token ids, or the previous slice's hidden states
        if self.first:
            inputs = {"input_ids": torch.tensor([message["tokens"]], device=self.device)}
        else:
            length, width = message["shape"]
            if width != self.model.config.hidden_size or len(payload) != length * width * 2:
                raise ValueError("hidden states don't match this model")
            hidden = torch.frombuffer(bytearray(payload), dtype=torch.bfloat16).view(1, length, width)
            inputs = {"inputs_embeds": hidden.to(self.device)}

        # Out: hidden states for the next slice, or the next token from the last
        with torch.no_grad():
            hidden = self.model.model(**inputs, past_key_values=self.caches[job], use_cache=True).last_hidden_state
            if not self.last:
                return ({"type": "hidden", "job": job, "shape": list(hidden.shape[1:])},
                        hidden[0].view(torch.int16).cpu().numpy().tobytes())
            logits = self.model.lm_head(hidden[0, -1]).float().cpu()
        return {"type": "token", "job": job, "token": self.sample(job, logits, message["sample"])}, b""

    def sample(self, job, logits, settings):
        import torch

        if settings["temperature"] == 0:
            return int(logits.argmax())
        if job not in self.generators:
            self.generators[job] = torch.Generator().manual_seed(settings["seed"])

        # Top-p: the likeliest tokens until they cover top_p of the probability
        probs = torch.softmax(logits / settings["temperature"], dim=-1)
        ranked, order = probs.sort(descending=True)
        ranked[ranked.cumsum(-1) - ranked > settings["top_p"]] = 0
        return int(order[torch.multinomial(ranked, 1, generator=self.generators[job])])

    def end(self, job):
        self.caches.pop(job, None)
        self.generators.pop(job, None)


def serve(slice):
    """Run what the CLI relays until it lets go (stdin closes)."""
    while received := frames.read(sys.stdin.buffer):
        message, payload = received
        job = message.get("job")

        if message.get("type") == "end":
            slice.end(job)
        elif message.get("type") == "forward":
            try:
                say(*slice.forward(message, payload))
            except Exception as exc:  # bad input, out of memory, ...
                slice.end(job)
                say({"type": "error", "job": job, "message": str(exc)[:500]})


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--start", type=int, required=True)
    parser.add_argument("--end", type=int, required=True)
    parser.add_argument("--package", action="append", default=[], help="name==version to install (repeat)")
    parser.add_argument("--device", default=os.environ.get("SINFERENCE_DEVICE") or "cuda", help="cuda, xpu (Intel), or cpu")
    parser.add_argument("--here", action="store_true", help="Use this Python's packages (development)")
    args = parser.parse_args()

    # PyTorch etc. live in the worker's own environment, at the versions the orchestrator sent
    if not (args.here or os.environ.get("SINFERENCE_WORKER_ENV")):
        if not args.package or not all(PACKAGE.fullmatch(package) for package in args.package):
            say({"type": "failed", "message": "The orchestrator didn't send valid packages to install."})
            return 1
        try:
            ensure_environment(args.package, args.device, sys.argv[1:])  # doesn't return
        except (OSError, subprocess.CalledProcessError) as exc:
            say({"type": "failed", "message": f"Couldn't set up the worker environment: {exc}"})
            return 1

    # Keep the CLI's channel to ourselves; anything else printed goes to the terminal
    global channel
    channel = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)

    try:
        import torch
    except ImportError:
        say({"type": "failed", "message": f"PyTorch is missing; delete {VENV} and try again."})
        return 1

    device = torch.device(args.device)
    if device.type == "cuda" and not torch.cuda.is_available():
        say({"type": "failed", "message": "No CUDA GPU available."})
        return 1
    if device.type == "xpu" and not torch.xpu.is_available():
        say({"type": "failed", "message": "PyTorch can't use this Intel GPU. It needs an Arc, Core Ultra or Data Center "
                                          "GPU w/ Intel's compute runtime and Level Zero loader installed "
                                          "(Arch: intel-compute-runtime level-zero-loader)."})
        return 1

    try:
        model = load_slice(args.repo, args.start, args.end, device)
    except Exception as exc:  # download, memory, or format problems all end the same way
        say({"type": "failed", "message": str(exc)})
        return 1

    say({"type": "ready"})
    serve(Slice(model, args.start == 0, args.end == model.config.num_hidden_layers, device))
    return 0


if __name__ == "__main__":
    sys.exit(main())
