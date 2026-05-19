#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ADMIN_KEY="$(tr -d '\r\n' < "$ROOT/conf/admin.key")"
APISIX_ENV="$ROOT/.env"
if [[ ! -f "$APISIX_ENV" ]]; then
  echo "missing $APISIX_ENV" >&2
  echo "Create it with OLLAMA_CLOUD_KEY_1, optional OLLAMA_CLOUD_KEY_2, and SILICONFLOW_CN_API_KEY." >&2
  exit 1
fi
set -a
# shellcheck disable=SC1090
source "$APISIX_ENV"
set +a

need() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    echo "missing required env var $name in $APISIX_ENV" >&2
    exit 1
  fi
}
need OLLAMA_CLOUD_KEY_1
need SILICONFLOW_CN_API_KEY

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

TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

python - <<'PY' "$TMPDIR" "$OLLAMA_CLOUD_KEY_1" "${OLLAMA_CLOUD_KEY_2:-}" "$SILICONFLOW_CN_API_KEY"
import json, sys
from pathlib import Path
out = Path(sys.argv[1])
ollama1, ollama2, sf = sys.argv[2], sys.argv[3], sys.argv[4]

def dump(name, obj):
    (out / name).write_text(json.dumps(obj, separators=(",", ":")))

def pool_route(route_id, name, public_model, instances, priority):
    return {
        "id": route_id,
        "name": name,
        "uri": "/v1/chat/completions",
        "methods": ["POST"],
        "priority": priority,
        # One function, one path: every model is selected by the request body's
        # public model id and then routed through an ai-proxy-multi pool. A
        # single-provider/single-key case is still represented as a one-instance
        # pool so LB/fallback/health/capability logic stays unified.
        "vars": [["post_arg.model", "==", public_model]],
        "plugins": {
            "ai-proxy-multi": {
                "instances": instances,
                "balancer": {"algorithm": "roundrobin"},
                "fallback_strategy": ["http_429", "http_5xx"],
                "timeout": 600000,
                "ssl_verify": True,
                "keepalive": True,
                "keepalive_timeout": 60000,
                "keepalive_pool": 30,
            }
        },
    }

ollama_instances = [{
    "name": "ollama-cloud-1",
    "provider": "openai-compatible",
    "weight": 1,
    "auth": {"header": {"Authorization": "Bearer " + ollama1}},
    "options": {"model": "glm-5.1"},
    "override": {"endpoint": "https://ollama.com/v1/chat/completions"},
}]
if ollama2:
    ollama_instances.append({
        "name": "ollama-cloud-2",
        "provider": "openai-compatible",
        "weight": 1,
        "auth": {"header": {"Authorization": "Bearer " + ollama2}},
        "options": {"model": "glm-5.1"},
        "override": {"endpoint": "https://ollama.com/v1/chat/completions"},
    })

vision_instances = [{
    "name": "siliconflow-cn-qwen-vision-1",
    "provider": "openai-compatible",
    "weight": 1,
    "auth": {"header": {"Authorization": "Bearer " + sf}},
    "options": {"model": "Qwen/Qwen3.6-35B-A3B"},
    "override": {"endpoint": "https://api.siliconflow.cn/v1/chat/completions"},
}]

pools = [
    {
        "id": "pool-ollama-glm-5-1",
        "public_model": "ollama/glm-5.1",
        "owned_by": "apisix-ollama-cloud",
        "route_name": "APISIX pool -> Ollama Cloud GLM-5.1",
        "instances": ollama_instances,
        "priority": 100,
    },
    {
        "id": "pool-siliconflow-qwen-vision",
        "public_model": "siliconflow-cn/Qwen/Qwen3.6-35B-A3B",
        "owned_by": "siliconflow-cn",
        "route_name": "APISIX pool -> SiliconFlow Qwen vision",
        "instances": vision_instances,
        "priority": 100,
    },
]

for pool in pools:
    dump(
        f"route-{pool['id']}.json",
        pool_route(
            pool["id"],
            pool["route_name"],
            pool["public_model"],
            pool["instances"],
            pool["priority"],
        ),
    )

models_payload = {
    "object": "list",
    "data": [
        {"id": pool["public_model"], "object": "model", "owned_by": pool["owned_by"]}
        for pool in pools
    ],
}
dump("route-main-models.json", {
    "id": "main-models",
    "name": "OpenAI-compatible model list generated from APISIX pools",
    "uri": "/v1/models",
    "methods": ["GET"],
    "plugins": {"mocking": {
        "content_type": "application/json",
        "response_status": 200,
        "with_mock_header": False,
        "response_example": json.dumps(models_payload),
    }},
})
PY

api_put pool-ollama-glm-5-1 "$TMPDIR/route-pool-ollama-glm-5-1.json"
api_put pool-siliconflow-qwen-vision "$TMPDIR/route-pool-siliconflow-qwen-vision.json"
api_put main-models "$TMPDIR/route-main-models.json"

# Remove historical split-provider/direct-route surfaces. The clean gateway has
# one OpenAI-compatible surface; every model request enters /v1 and resolves to a pool.
api_delete main-chat || true
api_delete vision-chat || true
api_delete vision-models || true
api_delete main-model-info-v1 || true
api_delete main-model-info-root || true

echo "APISIX AI gateway routes configured with unified pool routing and no LiteLLM shims."
