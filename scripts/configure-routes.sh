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

deploy_routes() {
  python3 "$ROOT/scripts/deploy-routes.py" \
    --registry "$ROOT/conf/model-pools.json" \
    --capabilities "$ROOT/conf/model-capabilities.json" \
    --admin-key-file "$ROOT/conf/admin.key" \
    --admin-url "http://127.0.0.1:9180" \
    "$@"
}

# First deploy ensures the local APISIX server publishes the current /v1/models
# catalog. Then refresh the SiliconFlow CN capability fallback from OpenRouter's
# catalog (same aggregator-style model IDs) and deploy once more so
# /v1/model-capabilities serves the refreshed registry.
deploy_routes "$@"
python3 "$ROOT/scripts/build-model-capabilities.py" \
  --base "$ROOT/conf/model-capabilities.json" \
  --output "$ROOT/conf/model-capabilities.json" \
  --openrouter "https://openrouter.ai/api/v1/models" \
  --public-catalog "http://127.0.0.1:4000/v1/models"
deploy_routes "$@"
