# Tables

Everything is in one PostgreSQL database (`DATABASE_URL`: local TimescaleDB, or
hosted Tiger Data). There's no migration tool: each service creates its own tables
when it starts, w/ `CREATE TABLE IF NOT EXISTS` + a few one-off fixes (`ALTER
TABLE … ADD COLUMN IF NOT EXISTS`). Foreign keys set the start order, which
`scripts/start.sh` follows: auth.py → nodes backend → orchestrator.

Rows below are illustrative; hashes and ids are shortened, times are Unix seconds.

## auth.py: accounts and sign-in

**users**: one per account (it can own nodes and call the API)

| id | username | created_at |
| --- | --- | --- |
| `8cdb…64cb` | alice | 1792290000 |

**password_credentials**

| user_id | salt | hash (scrypt) |
| --- | --- | --- |
| `8cdb…64cb` | `a91f…` | `3c0e…` |

**sessions**: browser + node sign-ins; only the token's hash is stored

| token_hash | user_id | kind | expires_at |
| --- | --- | --- | --- |
| `3f9a…` | `8cdb…64cb` | browser | 1792376400 |
| `b27e…` | `8cdb…64cb` | node | 1794882000 |

**device_logins**: a terminal sign-in waiting for browser approval (short-lived)

| device_hash | user_code | device_name | expires_at | last_poll | user_id |
| --- | --- | --- | --- | --- | --- |
| `55d0…` | ABCD-2345 | gpu-box | 1792290600 | 1792290012 | null until approved |

**rate_limits**

| bucket (hashed) | window_started_at | attempts |
| --- | --- | --- |
| `e3b0…` | 1792290000 | 3 |

**api_keys**: for the OpenAI-compatible API; only the key's hash is stored

| id | key_hash | user_id | name | hint | created_at | last_used |
| --- | --- | --- | --- | --- | --- | --- |
| `4be1…e871` | `91c2…` | `8cdb…64cb` | laptop | si-3p_…cbP0 | 1792290095 | 1792291200 |

## Nodes backend: models, nodes, wallets

**models**: the catalog, rewritten from `nodes/catalog.py` at every start; prices in
USD per million tokens

| id | name | repo | memory_gb | prompt_price | completion_price |
| --- | --- | --- | --- | --- | --- |
| qwen2.5-7b-instruct | Qwen2.5 7B Instruct | Qwen/Qwen2.5-7B-Instruct | 15.2 | 0.1 | 0.2 |

**nodes**: one per node session (a new sign-in = a new id); all set `offline` when
the backend starts

| id | session_hash | user_id | name | ip | vram_total_mb | vram_free_mb | model | status | connected_at | last_seen |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `0100…c05b` | `b27e…` | `8cdb…64cb` | sinference-node-1 | 207.148.17.19 | 15356 | 14963 | qwen2.5-7b-instruct | ready | 1792310000 | 1792311185 |
| `f107…2810` | `c81a…` | `77aa…` | archlinux | 99.234.33.50 | 8192 | 8192 | qwen2.5-7b-instruct | ready | 1792310100 | 1792311190 |

**wallets**: one per account: the wallet all its nodes stake from + get paid to.
`secret` is its private key, kept so the coordinator can stake for the nodes
(devnet only)

| user_id | address | linked_at | secret |
| --- | --- | --- | --- |
| `8cdb…64cb` | `6Zwt…ZzuA` | 1792309000 | `…` |

## Orchestrator: slices, jobs, stakes, bans

**slices**: which layers each node holds

| node_id | model | replica | start_layer | end_layer | loaded |
| --- | --- | --- | --- | --- | --- |
| `0100…c05b` | qwen2.5-7b-instruct | 0 | 0 | 27 | true |
| `f107…2810` | qwen2.5-7b-instruct | 0 | 27 | 28 | true |

**jobs**: one per chat request (`user_id`: the client); still `running` at a
restart = `failed`

| id | user_id | model | status | prompt_tokens | completion_tokens | audited | record | payout | created_at | finished_at |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `7ce3…98b9` | `8cdb…64cb` | qwen2.5-7b-instruct | done | 91 | 101 | false | `{"job":…}` | `4F3Z…S6F` | 1792311000 | 1792311022 |

`record` is the canonical JSON whose SHA-256 goes on chain w/ the payout:

```json
{"job": "7ce3…98b9", "model": "qwen2.5-7b-instruct", "layers": 28,
 "tokens": {"prompt": 91, "completion": 101}, "usd": 0.0000293, "sol_usd": 103.5, "lamports": 283,
 "prompt": "a1b2…", "output": "c3d4…", "boundaries": ["94a2…"],
 "parts": [{"node": "0100…c05b", "owner": "8cdb…64cb", "wallet": "6Zwt…ZzuA", "layers": [0, 27], "seconds": 10.4, "lamports": 283},
           {"node": "f107…2810", "owner": "77aa…", "wallet": "Hk74…Yi9b", "layers": [27, 28], "seconds": 11.6, "lamports": 0}]}
```

**job_parts**: each node's share of a job (not tied to `nodes`, so history survives a
logout); `released` is the release transaction that sent it on to the wallet

| job_id | node_id | user_id | wallet | start_layer | end_layer | lamports | released |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `7ce3…98b9` | `0100…c05b` | `8cdb…64cb` | `6Zwt…ZzuA` | 0 | 27 | 283 | `2xQd…` (after 10 min) |
| `7ce3…98b9` | `f107…2810` | `77aa…` | `Hk74…Yi9b` | 27 | 28 | 0 | null |

**stakes**: stakes that may still hold collateral; `emptied` once everything went
back to the wallet (the account's nodes all left, or it switched wallets)

| wallet | user_id | emptied |
| --- | --- | --- |
| `6Zwt…ZzuA` | `8cdb…64cb` | false |

**bans**: written by `verdict.py`

| user_id | wallet | job_id | node_id | slash | banned_at |
| --- | --- | --- | --- | --- | --- |
| `77aa…` | `HZ5N…DmZ7n` | `4803…0bc1` | `f2ff…c6ce` | `4Fvt…qLk` | 1792293500 |

## Outside the database

- Audit evidence: files in `data/audits/<job>/` (see inference.md).
- Stake balances: the program's `Stake` accounts on Solana (see solana.md).

## How long each node takes

Per node, not per layer: each job's `record` has `parts[].seconds`, a node's time on
the job's steps, round trips through the orchestrator included. Tokens/s and each
node's milliseconds per token:

```sql
SELECT id, completion_tokens,
       round(completion_tokens / sum((part->>'seconds')::numeric), 2) AS tokens_per_second,
       string_agg(part->'layers'->>0 || '-' || (part->'layers'->>1) || ': '
                  || round(1000 * (part->>'seconds')::numeric / completion_tokens) || ' ms/token', ', ') AS per_node
FROM jobs, json_array_elements(record::json->'parts') AS part
WHERE status = 'done' AND completion_tokens > 0
GROUP BY id, completion_tokens, created_at ORDER BY created_at DESC LIMIT 20;
```
