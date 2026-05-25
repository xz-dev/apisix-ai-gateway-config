#!/bin/sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"

python3 "$ROOT/scripts/verify-gateway.py" \
  --admin-key-file "$ROOT/conf/admin.key" \
  --admin-url "http://127.0.0.1:9180" \
  --gateway-url "http://127.0.0.1:4000" \
  "$@"
