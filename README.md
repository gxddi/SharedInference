# SharedInference

Distributed inference across spare GPUs. Models are split by layer across computers and served through an OpenAI-compatible API.

Contributors stake and receive payments in SOL on Solana devnet. Built with Python, PyTorch, PostgreSQL/TimescaleDB, and a C++ web server.

## Set up (inference testing)

Requires Python 3, an API key, and a model ready on the network. No Python packages to install for the chat test.

```bash
git clone https://github.com/gxddi/SharedInference
cd SharedInference
python3 tests/chat.py
```

Open [localhost:8765](http://localhost:8765), paste your API key, load models, and select one to chat. The counter shows output tokens per second, including request latency.

The default server is `https://66-42-90-154.sslip.io:1025`. Create a key on its [/keys/ page](https://66-42-90-154.sslip.io:1025/keys/).

To use another server:

```bash
python3 tests/chat.py --base-url https://your-server/api/v1
```

## Motivations

Run open-weight models without needing all their memory on one computer, and pay people for the compute they share.

More on [inference](docs/inference.md) and [settlement](docs/solana.md).

---

#### Made with 🧠
