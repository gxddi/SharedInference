#!/bin/bash
# Project-local native installation. No Docker, sudo, or system service.
set -euo pipefail
project=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
prefix="$project/sinference/.local/postgres"
python="$project/sinference/.venv/bin/python"
if [[ ! -x "$python" ]]; then
  echo 'Create sinference/.venv and install requirements.txt first.' >&2
  exit 1
fi

if [[ ! -x "$prefix/bin/postgres" || ! -f "$prefix/share/extension/timescaledb.control" ]]; then
  for command in curl sha256sum tar cmake gcc make perl; do
    command -v "$command" >/dev/null || { echo "Missing build dependency: $command" >&2; exit 1; }
  done
  build_root=$(mktemp -d /tmp/si-native-db.XXXXXX)
  echo "Building PostgreSQL 16.15 and TimescaleDB 2.30.2; logs: $build_root"
  curl -fLsS --connect-timeout 20 --max-time 300 https://ftp.postgresql.org/pub/source/v16.15/postgresql-16.15.tar.bz2 -o "$build_root/postgresql.tar.bz2"
  printf '%s  %s\n' c1575341fa7bd40f5274ea465b34390f4dc64cdd0770af327005caaeb9f6b7ed "$build_root/postgresql.tar.bz2" | sha256sum -c -
  curl -fLsS --connect-timeout 20 --max-time 300 https://api.github.com/repos/timescale/timescaledb/tarball/2.30.2 -o "$build_root/timescaledb.tar.gz"
  mkdir "$build_root/postgresql" "$build_root/timescaledb"
  tar -xjf "$build_root/postgresql.tar.bz2" -C "$build_root/postgresql" --strip-components=1
  tar -xzf "$build_root/timescaledb.tar.gz" -C "$build_root/timescaledb" --strip-components=1
  cd "$build_root/postgresql"
  ./configure --prefix="$prefix" --without-readline --without-icu --with-ssl=openssl > "$build_root/postgresql-configure.log" 2>&1 || { tail -50 "$build_root/postgresql-configure.log"; exit 1; }
  make -j2 > "$build_root/postgresql-build.log" 2>&1 || { tail -70 "$build_root/postgresql-build.log"; exit 1; }
  make install > "$build_root/postgresql-install.log" 2>&1 || { tail -50 "$build_root/postgresql-install.log"; exit 1; }
  cmake -S "$build_root/timescaledb" -B "$build_root/timescaledb-build" -DPG_CONFIG="$prefix/bin/pg_config" -DCMAKE_BUILD_TYPE=Release -DREGRESS_CHECKS=OFF -DTAP_CHECKS=OFF -DWARNINGS_AS_ERRORS=OFF > "$build_root/timescaledb-configure.log" 2>&1 || { tail -80 "$build_root/timescaledb-configure.log"; exit 1; }
  cmake --build "$build_root/timescaledb-build" -j2 > "$build_root/timescaledb-build.log" 2>&1 || { tail -80 "$build_root/timescaledb-build.log"; exit 1; }
  cmake --install "$build_root/timescaledb-build" > "$build_root/timescaledb-install.log" 2>&1 || { tail -60 "$build_root/timescaledb-install.log"; exit 1; }
  cd "$project"
  rm -rf "$build_root"
fi

exec "$python" "$project/scripts/misc/local-db.py" init
