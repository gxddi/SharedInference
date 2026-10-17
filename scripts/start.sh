#!/bin/bash
# Builds what's needed, then runs the whole server side:
#   local database (if .env uses it) → auth.py → node backend → orchestrator (+ OpenAI API) → server.cpp
# The first run sets up a local database (scripts/misc/), and asks for what sinference/.env doesn't
# have yet: the site's public address, and the Solana program + coordinator key. Answers can also
# come from the environment: SINFERENCE_PUBLIC_URL, SOLANA_PROGRAM, SOLANA_KEYPAIR (a path)
# The TLS certificate + key go in site/creds/ (localhost+2.pem, localhost+2-key.pem) beforehand.
# Ctrl-C stops everything.
set -euo pipefail

project=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project"
python=sinference/.venv/bin/python
env=sinference/.env
key=data/solana/authority.json  # the coordinator's, for payouts + slashes


###########
# Helpers #
###########

port_open() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }

wait_for() { # port, name
  for _ in $(seq 100); do
    port_open "$1" && return 0
    sleep .1
  done
  echo "$2 didn't start on port $1." >&2
  exit 1
}

run() { # name, command... → background, w/ every line prefixed by [name]
  local name=$1
  shift
  "$@" 2>&1 | sed -u "s/^/[$name] /" &
}

has() { grep -q "^$1=" "$env" 2>/dev/null; } # set in .env, even to nothing

setting() { # key → its value in .env
  sed -n "s/^$1=//p" "$env" 2>/dev/null | tail -1 | sed 's/^"\(.*\)"$/\1/'
}

ask() { # prompt, default → the answer, or the default w/o a terminal to ask on
  local answer=
  if [[ -t 0 ]]; then read -rp "$1" answer; fi
  echo "${answer:-$2}"
}


#########
# Build #
#########

# Python environment for sinference/
if [[ ! -x $python ]]; then
  echo 'Creating sinference/.venv...'
  python3 -m venv sinference/.venv
  "$python" -m pip install --quiet -r requirements.txt
fi

# C++ server, only when its sources changed
if [[ ! -x site/server || site/server.cpp -nt site/server || -n $(find site/server_tools -newer site/server) ]]; then
  echo 'Building site/server...'
  (cd site && g++ -std=c++20 server.cpp -o server -lssl -lcrypto -lcurl)
fi


############
# Settings #
############

umask 077  # .env + the key are secrets
touch "$env"

# Database: a local one, built + set up here (a Tiger Data DATABASE_URL in .env is used instead)
if [[ -z $(setting DATABASE_URL) ]]; then
  bash scripts/misc/setup-local-db.sh
fi

# Where the site is reached from; nodes connect to its host, port 1027
if ! has PUBLIC_URL; then
  url=${SINFERENCE_PUBLIC_URL:-$(ask 'Public address of this site [https://localhost:1025]: ' https://localhost:1025)}
  printf 'PUBLIC_URL=%s\n' "$url" >> "$env"
fi
public_url=${SINFERENCE_PUBLIC_URL:-$(setting PUBLIC_URL)}

# Solana: the deployed program's address (nothing = run w/o stakes or payouts), and the coordinator's key
if ! has SOLANA_PROGRAM; then
  printf 'SOLANA_PROGRAM=%s\n' "${SOLANA_PROGRAM:-$(ask 'Solana program address, or Enter to run without Solana: ' '')}" >> "$env"
fi
if [[ -n $(setting SOLANA_PROGRAM) && ! -f $key ]]; then
  given=${SOLANA_KEYPAIR:-$(ask "Path to the coordinator's keypair file (it signs payouts + slashes): " '')}
  if [[ ! -f $given ]]; then
    echo "No coordinator key. Put its keypair file at $key, or remove SOLANA_PROGRAM from $env." >&2
    exit 1
  fi
  mkdir -p "$(dirname "$key")"
  cp "$given" "$key"
fi
umask 022


#########
# Start #
#########

# Nothing else should already be on our ports
for port in 1025 1026 1027 1028 1029 1030; do
  if port_open "$port"; then
    echo "Port $port is already in use. Stop whatever is running there first." >&2
    exit 1
  fi
done

# Stop every child when this script exits (Ctrl-C, or any service dying)
trap 'trap - EXIT INT TERM; kill 0 2>/dev/null' EXIT INT TERM

# Local database, if that's what .env points at
uses_local_db=$("$python" - <<'EOF'
from dotenv import dotenv_values
from psycopg.conninfo import conninfo_to_dict
url = dotenv_values("sinference/.env", interpolate=False).get("DATABASE_URL") or ""
info = conninfo_to_dict(url)
print("yes" if (info.get("host"), info.get("port")) == ("127.0.0.1", "6543") else "no")
EOF
)
if [[ $uses_local_db == yes ]]; then
  "$python" scripts/misc/local-db.py start
fi

# auth.py first: it creates the users + sessions tables the node backend needs
run auth "$python" -u sinference/backend/auth.py --public-url "$public_url"
wait_for 1026 'auth.py'

run nodes "$python" -u sinference/backend/nodes --public-url "$public_url"
wait_for 1028 'Nodes API'
wait_for 1027 'Node connections'

# Orchestrator dials the node backend's link on 1029 (retrying until it's up), serves the API on 1030
run orchestrator "$python" -u sinference/backend/orchestrator
wait_for 1030 'OpenAI-compatible API'

run site bash -c 'cd site && exec ./server'
wait_for 1025 'server.cpp'

echo
echo "Everything is up: $public_url  (Ctrl-C stops it all)"
echo

# Stay up until any one of them stops
wait -n
echo 'A service stopped; shutting the rest down.' >&2
exit 1
