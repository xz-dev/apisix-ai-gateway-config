#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ADMIN_KEY="$(tr -d '\r\n' < "$ROOT/conf/admin.key")"
APISIX_ENV="$ROOT/.env"
LITELLM_ENV="${LITELLM_ENV:-$HOME/.config/litellm/litellm.env}"

if [[ ! -f "$APISIX_ENV" ]]; then
  echo "missing $APISIX_ENV" >&2
  echo "Create it with OLLAMA_CLOUD_KEY_1, optional OLLAMA_CLOUD_KEY_2, DEEPSEEK_API_KEY, XAI_API_KEY, and SILICONFLOW_CN_API_KEY." >&2
  exit 1
fi

set -a
# Keep APISIX-local .env authoritative, but source the historical LiteLLM env
# first during migration so missing keys can be reused without printing secrets.
if [[ -f "$LITELLM_ENV" ]]; then
  # shellcheck disable=SC1090
  source "$LITELLM_ENV"
fi
# shellcheck disable=SC1090
source "$APISIX_ENV"
set +a

TMPDIR="$(mktemp -d)"
MANIFEST="$TMPDIR/manifest.json"
trap 'rm -rf "$TMPDIR"' EXIT

python3 "$ROOT/scripts/render-routes.py" \
  --registry "$ROOT/conf/model-pools.json" \
  --out-dir "$TMPDIR" \
  --manifest "$MANIFEST"

api_put() {
  local id="$1" json="$2"
  curl -fsS "http://127.0.0.1:9180/apisix/admin/routes/$id" \
    -H "X-API-KEY: $ADMIN_KEY" \
    -H 'Content-Type: application/json' \
    -X PUT --data-binary @"$json" >/dev/null
  echo "configured route $id"
}

api_delete() {
  local id="$1"
  local status
  status="$(curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:9180/apisix/admin/routes/$id" \
    -H "X-API-KEY: $ADMIN_KEY" \
    -X DELETE)"
  case "$status" in
    200|202|204) echo "deleted route $id" ;;
    404) echo "route $id already absent" ;;
    *) echo "failed to delete route $id: HTTP $status" >&2; return 1 ;;
  esac
}

mapfile -t DESIRED_IDS < <(python3 - "$MANIFEST" <<'PY'
import json, sys
for rid in json.load(open(sys.argv[1]))['route_ids']:
    print(rid)
PY
)

for id in "${DESIRED_IDS[@]}"; do
  api_put "$id" "$TMPDIR/route-$id.json"
done

# Delete stale routes previously managed by this repo but no longer generated.
CURRENT_ROUTES="$TMPDIR/current-routes.json"
curl -fsS http://127.0.0.1:9180/apisix/admin/routes \
  -H "X-API-KEY: $ADMIN_KEY" > "$CURRENT_ROUTES"
python3 - "$CURRENT_ROUTES" "$MANIFEST" <<'PY' > "$TMPDIR/stale-route-ids.txt"
import json, sys
current = json.load(open(sys.argv[1])).get('list') or []
desired = set(json.load(open(sys.argv[2]))['route_ids'])
for item in current:
    route = item.get('value') or item
    labels = route.get('labels') or {}
    rid = route.get('id')
    if labels.get('managed-by') == 'apisix-ai-gateway-config' and rid not in desired:
        print(rid)
PY
while IFS= read -r id; do
  [[ -n "$id" ]] && api_delete "$id"
done < "$TMPDIR/stale-route-ids.txt"

# Remove historical split-provider/direct-route surfaces and LiteLLM metadata shims.
for old_id in main-chat vision-chat vision-models main-model-info-v1 main-model-info-root tmp-regex-post-arg-test; do
  api_delete "$old_id" || true
done

python3 - "$MANIFEST" <<'PY'
import json, sys
m = json.load(open(sys.argv[1]))
print(f"APISIX AI gateway routes configured: {m['model_count']} public models, {len(m['route_ids'])} managed routes, unified /v1 pool routing, no LiteLLM shims.")
PY
