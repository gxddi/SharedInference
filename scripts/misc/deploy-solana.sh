#!/bin/bash
# Builds the stakes program (sinference/solana/), deploys it to devnet, initializes it,
# and points sinference/.env at it. Rerunning upgrades the program in place.
#
#   Needs the Solana CLI 3.1.10 (https://release.anza.xyz/v3.1.10/install) and devnet SOL
#   on the coordinator's key for the program's rent: https://faucet.solana.com
set -euo pipefail

project=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$project"
export PATH="$HOME/.local/share/solana/install/active_release/bin:$PATH"
python=sinference/.venv/bin/python
rpc=${SOLANA_RPC:-https://api.devnet.solana.com}
keys=data/solana


########
# Keys #
########

# The coordinator's (pays fees + rent, signs payouts and slashes) and the program's address
mkdir -p "$keys"
for key in authority stakes-keypair; do
  [[ -f $keys/$key.json ]] || solana-keygen new --no-bip39-passphrase --silent -o "$keys/$key.json"
done
chmod 600 "$keys"/*.json
authority=$(solana-keygen pubkey "$keys/authority.json")
program=$(solana-keygen pubkey "$keys/stakes-keypair.json")

# The program's address goes in its source
sed -i "s/^declare_id!(\"[^\"]*\")/declare_id!(\"$program\")/" sinference/solana/programs/stakes/src/lib.rs
sed -i "s/^stakes = \"[^\"]*\"/stakes = \"$program\"/" sinference/solana/Anchor.toml


##########
# Deploy #
##########

(cd sinference/solana && cargo-build-sbf --manifest-path programs/stakes/Cargo.toml --sbf-out-dir target/deploy)

echo "Coordinator $authority has $(solana balance "$authority" --url "$rpc")."

# A bigger build needs room first (the loader grows a program by at least 10 KiB at a time)
size=$(stat -c %s sinference/solana/target/deploy/stakes.so)
if current=$(solana program show "$program" --url "$rpc" --keypair "$keys/authority.json" --output json 2>/dev/null |
             "$python" -c 'import json, sys; print(json.load(sys.stdin)["dataLen"])') && (( size > current )); then
  solana program extend "$program" $(( size - current > 10240 ? size - current : 10240 )) \
    --keypair "$keys/authority.json" --url "$rpc"
fi

solana program deploy sinference/solana/target/deploy/stakes.so --program-id "$keys/stakes-keypair.json" \
  --keypair "$keys/authority.json" --url "$rpc"

# Initialize (once), then point the services at it
"$python" - "$rpc" "$program" "$keys/authority.json" <<'PYTHON'
import sys
from dotenv import set_key
sys.path.insert(0, "sinference/backend/orchestrator")
from stakes import Stakes

rpc, program, keypair = sys.argv[1:]
stakes = Stakes(rpc, program, keypair, 0, None)
signature = stakes.initialize()
print(f"Initialized: {signature}" if signature else "Already initialized.")
print(f"Payout vault (fund it w/ a plain transfer): {stakes.config}")

for key, value in (("SOLANA_PROGRAM", program), ("SOLANA_RPC", rpc)):  # the key stays at its default path
    set_key("sinference/.env", key, value, quote_mode="never")
PYTHON

echo "Done. Restart scripts/start.sh to use program $program."
