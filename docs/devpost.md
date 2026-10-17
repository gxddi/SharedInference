## Inspiration

Hosting LLMs is expensive. 

Renting them out from companies, leaves you broke.

Your computer sits idle generating negative ROI everyday.

Isn't there some way to benefit from rapidly improving open-weight models, while sharing them with others?

SharedInference introduces a pipeline that brings decentralized, secure, and profitable inference to all.

## What it does

SharedInference connects people who need LLM inference with people who have spare GPU capacity. Contributors install our client, sign in, and choose an open-weight model to help serve. The orchestrator assigns each machine a consecutive range of model layers that fits its available memory. Several machines can collectively host a model that would not fit on any one of them.

Current implementation exposese an API key and sends requests to an OpenAI-compatible endpoint. Behind that endpoint, the orchestrator passes a request through the machines holding the model's layers and returns the generated answer. Our current catalog supports Qwen2.5 Instruct models from 0.5 to 7 billion parameters.

Contributors receive payments in Solana's native token, SOL, weighted by the layers they serve and their measured speed. Each participating node is backed by collateral, giving its owner something to lose if an audit establishes fraud. The prototype runs on Solana devnet: it demonstrates the payment mechanism with test tokens, while profitability after electricity and hardware costs remains to be measured.

## Sponsors!

**Solana — collateral and settlement.** We built and deployed a Rust program using Anchor that holds stakes, pays contributors, releases funds, and burns collateral when the coordinator submits a fraud verdict with smart contracts. Each payout includes a hash linking it to the corresponding inference record. The orchestrator automatically stakes the equivalent of 20 US dollars per participating node, and holds earnings through a ten-minute audit window before releasing them. The [program is deployed on devnet](https://explorer.solana.com/address/97VaN9bk5Ee57BFaW5UVoojHjhy3K16RvRcZEnu5cULU?cluster=devnet).

**Tiger Data — the shared datastore.** Accounts, connected nodes, model slices, jobs, and payment references live in PostgreSQL. Development uses native TimescaleDB, and the same connection interface supports hosted Tiger Data. Hosted deployment and time-series telemetry are still next steps; our current implementation uses relational tables.

**Vultr — the deployment target.** Our architecture separates the central services from independently installed workers, allowing cloud machines and personal computers to join through the same client. Vultr is our target for hosting the service and testing workers across separate instances; that deployment remains to be validated.

## How we built it

![SharedInference pipeline showing requests, compute nodes, TimescaleDB, Solana settlement, and the planned off-chain verifier](https://raw.githubusercontent.com/gxddi/SharedInference/2c65b7bdcefc0e8729bd50c8014f4c89604550da/docs/assets/htv-pipeline.png)

The website is built on low level c++ servers and native html, css and javascript. We built the model-slicing worker with PyTorch, Hugging Face Transformers, and safetensors. It reads the model's weight metadata, downloads the necessary weight files, and loads only the tensors belonging to its assigned layers. The orchestrator pins the worker's dependencies so every slice uses the same library versions.

For each generation step, the first slice receives token IDs, intermediate slices exchange hidden states through the orchestrator, and the last slice selects the next token. Each worker retains its attention cache between steps. The developer sees a normal chat completion through `POST /api/v1/chat/completions`.

Alongside inference, we record token counts, layer assignments, boundary hashes, timings, and each contributor's payment share. Prices come from our model catalog and are converted into SOL using Coinbase's spot price. A randomly selected ten percent of jobs also retain the inputs and intermediate data needed for a future replay verifier. Prompts and model tensors stay off-chain.

## Challenges we ran into

### Safety
You shouldn't trust strangers, but every friend was once a stranger too. Implementing a minimal trust framework for shared LLM inference proved to be very difficult.

Several methods fall short, like establishing computational proofs ([EZKL](https://docs.ezkl.xyz/) too computationally heavy), utilizing consensus (generating fake results is too easy), and we finally landed on a stake based system to penalize malicious actors.

For our prototype, we chose optimistic execution with hidden audit sampling. Agreement between workers alone cannot rule out collusion, and a hash commits to an output without proving that the model produced it. The intended verifier replays a sampled slice and compares its outputs; evidence collection and the slashing path are implemented, but automatic replay verification is still unfinished.

### Parallelism over wifi
Several parallel computational tricks exist for speeding up computations. But over long distances, it's hard. The only real viable option was pipeline parallelism.

Keeping consecutive layers together reduces communication to the boundaries between slices. Workers retain their attention caches locally, so generation does not require retransmitting the entire conversation at every step. Even so, every generated token still travels through every slice, and every boundary adds a network round trip through the orchestrator because individual nodes don't rely on port-forwarding (which is also why I couldn't use vLLM).

## Accomplishments that we're proud of

Getting an actual model through the complete request path. In our local end-to-end test, a >16gb Qwen2 model split across two workers produced the same greedy output as the unsplit model, with requests passing through the website's C++ server and orchestrator.

Connecting inference to settlement. We exercised staking, job payouts, and slashing on Solana devnet, rather than stopping at recording an output hash. The project also includes browser account management, device approval, model selection, API keys, and tests covering node registration, slice assignment, inference, and payment logic.

Building our own slice loader. A contributor's machine can load its portion of the model without allocating the full set of weights, which is essential to pooling memory across smaller GPUs.

## What we learned

Distributed inference requires decisions about memory, communication, and failure handling before it can benefit from extra compute. A worker being online does not mean its model is ready, and several ready slices are useful only when they cover the entire model.

We also learned to separate accountability from correctness. Public settlement records make payments inspectable, and collateral makes penalties possible, but neither identifies a fraudulent computation by itself. The verifier is essential to making the stake incentive meaningful.

Finally, sharing compute creates a privacy problem as well as a scheduling problem. Keeping prompts off-chain does not stop participating machines from seeing or retaining the data they process, and keeping audit evidence creates an explicit retention tradeoff.

## What's next for SharedInference

Complete the replay verifier and test it against deliberately corrupted intermediate results. We need to measure both detection accuracy and verification cost before claiming that the network can reliably penalize dishonest inference.

Explore zero data retention methods, including short-lived audit evidence and explicit deletion policies. A stronger privacy design must also address what participating machines can observe; a server-side deletion policy alone cannot guarantee that a contributor kept no copy.

Build a more flexible orchestrator that considers network latency, available memory, and measured throughput when assigning slices. Better balancing, scheduling concurrent requests, and recovering from departed workers are the next steps toward useful performance outside a local test.

Deploy and benchmark the network on Vultr, add time-series telemetry in Tiger Data, and compare contributor earnings against operating costs. Those measurements will tell us where shared inference is practical and what it takes to make participation worthwhile.
