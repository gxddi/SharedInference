"""
Chat messages → prompt tokens, and generated tokens → text, w/ each model's own tokenizer.

Only the tokenizer is downloaded (no weights, no PyTorch).
"""

import json
from pathlib import Path
import threading

from huggingface_hub import hf_hub_download
from transformers import AutoTokenizer


class Chat:
    def __init__(self):
        self.tokenizers = {}  # repo → tokenizer
        self.stops = {}       # repo → token ids that end a reply
        self.lock = threading.Lock()  # tokenizers aren't safe to share between threads

    def tokenizer(self, repo):
        if repo not in self.tokenizers:
            self.tokenizers[repo] = AutoTokenizer.from_pretrained(repo)
        return self.tokenizers[repo]

    def prompt(self, repo, messages):
        with self.lock:
            return self.tokenizer(repo).apply_chat_template(messages, add_generation_prompt=True, tokenize=True,
                                                            return_dict=False)

    def stop(self, repo):
        """End-of-sequence ids, from the model's generation config + tokenizer."""
        with self.lock:
            if repo not in self.stops:
                config = json.loads(Path(hf_hub_download(repo, "generation_config.json")).read_text())
                ids = config.get("eos_token_id") or []
                self.stops[repo] = {*(ids if isinstance(ids, list) else [ids]), self.tokenizer(repo).eos_token_id}
            return self.stops[repo]

    def text(self, repo, tokens):
        with self.lock:
            return self.tokenizer(repo).decode(tokens, skip_special_tokens=True)
