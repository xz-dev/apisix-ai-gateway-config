#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APISIX_ENV="$ROOT/.env"

if [[ ! -f "$APISIX_ENV" ]]; then
  echo "missing $APISIX_ENV" >&2
  echo "Create it with OLLAMA_CLOUD_KEY_1, optional OLLAMA_CLOUD_KEY_2, DEEPSEEK_API_KEY, XAI_API_KEY, and SILICONFLOW_CN_API_KEY." >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "$APISIX_ENV"
set +a

python3 "$ROOT/scripts/deploy-routes.py" \
  --registry "$ROOT/conf/model-pools.json" \
  --capabilities "$ROOT/conf/model-capabilities.json" \
  --admin-key-file "$ROOT/conf/admin.key" \
  --admin-url "http://127.0.0.1:9180" \
  "$@"
