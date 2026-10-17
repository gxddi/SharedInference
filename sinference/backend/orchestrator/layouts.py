"""How big each part of a model is, from its Hugging Face repo, w/o downloading the weights."""

import json
from dataclasses import dataclass
from pathlib import Path

from huggingface_hub import HfApi, hf_hub_download

BF16 = 2  # bytes per parameter, as nodes load it


@dataclass
class Layout:
    layers: int         # transformer layers (decoder blocks)
    layer_bytes: list   # bytes of each layer
    embed_bytes: int    # token embeddings, held by the first slice
    head_bytes: int     # final norm + LM head, held by the last slice


def from_hub(repo):
    config = json.loads(Path(hf_hub_download(repo, "config.json")).read_text())
    layers = config["num_hidden_layers"]
    layer_bytes, embed_bytes, head_bytes = [0] * layers, 0, 0
    has_head = False

    # Sum every tensor into its part (safetensors headers only, no weights)
    metadata = HfApi().get_safetensors_metadata(repo)
    for file in metadata.files_metadata.values():
        for name, tensor in file.tensors.items():
            size = tensor.parameter_count * BF16
            if name.startswith("model.layers."):
                layer_bytes[int(name.split(".")[2])] += size
            elif name.startswith("model.embed_tokens."):
                embed_bytes += size
            else:
                head_bytes += size  # model.norm, lm_head
                has_head |= name.startswith("lm_head.")

    # Tied embeddings: the last slice needs the embedding matrix as its head
    if not has_head:
        head_bytes += embed_bytes

    return Layout(layers, layer_bytes, embed_bytes, head_bytes)
