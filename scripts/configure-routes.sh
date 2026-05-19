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
out=Path(sys.argv[1])
ollama1, ollama2, sf = sys.argv[2], sys.argv[3], sys.argv[4]

def dump(name, obj):
    (out/name).write_text(json.dumps(obj, separators=(",", ":")))

ollama_instances=[{
    "name":"ollama-cloud-1",
    "provider":"openai-compatible",
    "weight":1,
    "auth":{"header":{"Authorization":"Bearer "+ollama1}},
    "options":{"model":"glm-5.1"},
    "override":{"endpoint":"https://ollama.com/v1/chat/completions"}
}]
if ollama2:
    ollama_instances.append({
        "name":"ollama-cloud-2",
        "provider":"openai-compatible",
        "weight":1,
        "auth":{"header":{"Authorization":"Bearer "+ollama2}},
        "options":{"model":"glm-5.1"},
        "override":{"endpoint":"https://ollama.com/v1/chat/completions"}
    })

dump('route-main-chat.json', {
    "id":"main-chat",
    "name":"OpenAI-compatible chat -> Ollama Cloud GLM-5.1",
    "uri":"/v1/chat/completions",
    "methods":["POST"],
    "plugins":{
        "ai-proxy-multi":{
            "instances":ollama_instances,
            "balancer":{"algorithm":"roundrobin"},
            "fallback_strategy":["http_429","http_5xx"],
            "timeout":600000,
            "ssl_verify":True,
            "keepalive":True,
            "keepalive_timeout":60000,
            "keepalive_pool":30
        }
    }
})

dump('route-vision-chat.json', {
    "id":"vision-chat",
    "name":"OpenAI-compatible chat -> SiliconFlow Qwen vision",
    "uri":"/siliconflow-cn/v1/chat/completions",
    "methods":["POST"],
    "plugins":{
        "ai-proxy":{
            "provider":"openai-compatible",
            "auth":{"header":{"Authorization":"Bearer "+sf}},
            "options":{"model":"Qwen/Qwen3.6-35B-A3B"},
            "override":{"endpoint":"https://api.siliconflow.cn/v1/chat/completions"},
            "timeout":600000,
            "ssl_verify":True,
            "keepalive":True,
            "keepalive_timeout":60000,
            "keepalive_pool":30
        }
    }
})

dump('route-main-models.json', {
    "id":"main-models",
    "name":"OpenAI-compatible model list for main APISIX endpoint",
    "uri":"/v1/models",
    "methods":["GET"],
    "plugins":{"mocking":{
        "content_type":"application/json",
        "response_status":200,
        "with_mock_header":False,
        "response_example":json.dumps({"object":"list","data":[{"id":"ollama/glm-5.1","object":"model","owned_by":"apisix-ollama-cloud"}]})
    }}
})

dump('route-vision-models.json', {
    "id":"vision-models",
    "name":"OpenAI-compatible model list for SiliconFlow APISIX endpoint",
    "uri":"/siliconflow-cn/v1/models",
    "methods":["GET"],
    "plugins":{"mocking":{
        "content_type":"application/json",
        "response_status":200,
        "with_mock_header":False,
        "response_example":json.dumps({"object":"list","data":[{"id":"siliconflow-cn/Qwen/Qwen3.6-35B-A3B","object":"model","owned_by":"apisix-siliconflow"}]})
    }}
})
PY

api_put main-chat "$TMPDIR/route-main-chat.json"
api_put vision-chat "$TMPDIR/route-vision-chat.json"
api_put main-models "$TMPDIR/route-main-models.json"
api_put vision-models "$TMPDIR/route-vision-models.json"

# This APISIX deployment is a clean AI gateway, not a LiteLLM compatibility layer.
# Ensure any historical LiteLLM-specific metadata shim routes are absent.
api_delete main-model-info-v1 || true
api_delete main-model-info-root || true

echo "APISIX AI gateway routes configured without LiteLLM compatibility shims."
