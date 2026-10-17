"""Models a node can choose to serve."""

# (id, name, Hugging Face repo, memory for the whole model in GB at bf16,
#  USD per million prompt tokens, per million completion tokens: all paid to the nodes serving it)
# Prices: the cheapest API provider on 2026-10-17 (pricepertoken.com). Nobody lists 3B
# Instruct, so it's priced like Qwen2.5 Coder 3B.
MODELS = [
    ("qwen2.5-0.5b-instruct", "Qwen2.5 0.5B Instruct", "Qwen/Qwen2.5-0.5B-Instruct", 1, .2, .2),
    ("qwen2.5-1.5b-instruct", "Qwen2.5 1.5B Instruct", "Qwen/Qwen2.5-1.5B-Instruct", 3.1, .1, .1),
    ("qwen2.5-3b-instruct", "Qwen2.5 3B Instruct", "Qwen/Qwen2.5-3B-Instruct", 6.2, .1, .1),
    ("qwen2.5-7b-instruct", "Qwen2.5 7B Instruct", "Qwen/Qwen2.5-7B-Instruct", 15.2, .1, .2),
]

IDS = [id for id, *_ in MODELS]
