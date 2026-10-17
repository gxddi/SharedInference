#!/bin/sh
# Installs the sinference node client (the model worker sets itself up on first use).
set -eu

# Requirements
if ! command -v python3 >/dev/null 2>&1; then
  echo 'Python 3.10 or newer is required.' >&2
  exit 1
fi
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || {
  echo 'Python 3.10 or newer is required.' >&2
  exit 1
}
python3 -c 'import ensurepip' 2>/dev/null || {  # the model worker makes its own environment
  echo 'Python needs venv support: on Debian/Ubuntu, sudo apt install python3-venv' >&2
  exit 1
}
command -v curl >/dev/null 2>&1 || { echo 'curl is required.' >&2; exit 1; }

# Where things come from and go (all overridable)
server=${SINFERENCE_SERVER:-https://localhost:1025}
server=${server%/}
repo_base=${SINFERENCE_REPO_BASE:-https://raw.githubusercontent.com/gxddi/SharedInference/main}
repo_base=${repo_base%/}
install_dir=${SINFERENCE_INSTALL_DIR:-"$HOME/.local/bin"}               # the command
client_dir="${XDG_DATA_HOME:-$HOME/.local/share}/sinference/client"    # its modules

# Download every module into a temp dir first, so a failed download changes nothing
mkdir -p "$install_dir" "$(dirname "$client_dir")"
temporary=$(mktemp -d "$(dirname "$client_dir")/.client.XXXXXX")
trap 'rm -rf "$temporary"' EXIT HUP INT TERM

for module in cli account connection frames hardware supervisor worker; do
  curl --fail --silent --show-error --location "$repo_base/sinferenced/$module.py" -o "$temporary/$module.py"
  python3 -c 'import ast, pathlib, sys; ast.parse(pathlib.Path(sys.argv[1]).read_text())' "$temporary/$module.py"
done
chmod 755 "$temporary/cli.py"
printf '%s\n' "$server" > "$temporary/server"  # so `sinference up` knows which site to join

# Swap in the new client, then point the command at it
rm -rf "$client_dir"
mv "$temporary" "$client_dir"
ln -sf "$client_dir/cli.py" "$install_dir/sinference"

printf '\nInstalled sinference for %s in %s\n' "$server" "$install_dir"
case ":$PATH:" in
  *":$install_dir:"*) ;;
  *) printf 'Add this directory to your PATH: %s\n' "$install_dir" ;;
esac
printf '\nNext, sign in and set up this node:\n  "%s/sinference" up\n' "$install_dir"
