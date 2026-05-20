#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ADMIN_KEY="$(tr -d '\r\n' < "$ROOT/conf/admin.key")"

echo '--- APISIX Admin API managed model routes ---'
ROUTES_TMP="$(mktemp)"
MODELS_TMP="$(mktemp)"
trap 'rm -f "${ROUTES_TMP:-}" "${MODELS_TMP:-}" "${TMP:-}" "${BODY:-}"' EXIT
curl -fsS http://127.0.0.1:9180/apisix/admin/routes \
  -H "X-API-KEY: $ADMIN_KEY" > "$ROUTES_TMP"
python3 - "$ROUTES_TMP" <<'PY'
import json, sys
payload = json.load(open(sys.argv[1]))
routes = payload.get('list') or []
managed = []
for item in routes:
    value = item.get('value') or item
    plugins = value.get('plugins') or {}
    labels = value.get('labels') or {}
    if 'ai-proxy' in plugins:
        raise SystemExit(f"direct ai-proxy route violates unified pool routing: {value.get('id')}")
    if labels.get('managed-by') == 'apisix-ai-gateway-config':
        managed.append(value)
ids = {r.get('id') for r in managed}
if 'main-models' not in ids:
    raise SystemExit('missing managed /v1/models catalog route')
if 'main-model-capabilities' not in ids:
    raise SystemExit('missing managed /v1/model-capabilities route')
pool_routes = [r for r in managed if (r.get('labels') or {}).get('route-kind') == 'model-pool']
if len(pool_routes) < 40:
    raise SystemExit(f'expected managed provider pools, got only {len(pool_routes)}')
for r in pool_routes:
    plugins = r.get('plugins') or {}
    multi = plugins.get('ai-proxy-multi') or {}
    if r.get('uri') != '/v1/chat/completions' or 'ai-proxy-multi' not in plugins:
        raise SystemExit(f"managed model route is not an ai-proxy-multi chat pool: {r.get('id')}")
    if multi.get('fallback_strategy') != ['http_429', 'http_5xx']:
        raise SystemExit(f"route missing 429/5xx fallback strategy: {r.get('id')}")
summary = {
    'managed_route_count': len(managed),
    'pool_route_count': len(pool_routes),
    'sample_route_ids': sorted(ids)[:8],
}
# Check the Ollama Cloud LB/fallback pool has both keys when configured.
ollama = next((r for r in pool_routes if (r.get('labels') or {}).get('public-model') == 'ollama/glm-5.1'), None)
if not ollama:
    raise SystemExit('missing ollama/glm-5.1 pool')
inst = ((ollama.get('plugins') or {}).get('ai-proxy-multi') or {}).get('instances') or []
if len(inst) < 2:
    raise SystemExit(f'ollama/glm-5.1 should have two configured Ollama Cloud instances, got {len(inst)}')
if sorted({i.get('priority', 0) for i in inst}) != [0]:
    raise SystemExit('Ollama Cloud load-balancing instances should share priority 0')
# Check xAI fallback-provider pool uses lower priority than primary provider routes.
xai = next((r for r in pool_routes if (r.get('labels') or {}).get('public-model') == 'xai/grok-4.3'), None)
if not xai:
    raise SystemExit('missing xai/grok-4.3 fallback-provider pool')
xai_inst = (((xai.get('plugins') or {}).get('ai-proxy-multi') or {}).get('instances') or [{}])[0]
if xai_inst.get('priority') != 10:
    raise SystemExit('xAI fallback-provider instance should use priority 10')
print(json.dumps(summary, ensure_ascii=False, indent=2))
PY

echo '--- /v1/models public catalog ---'
curl -fsS http://127.0.0.1:4000/v1/models > "$MODELS_TMP"
python3 - "$MODELS_TMP" <<'PY'
import json, sys
payload = json.load(open(sys.argv[1]))
ids = [item.get('id') for item in payload.get('data') or [] if isinstance(item, dict)]
required = {
    'ollama/glm-5.1',
    'deepseek/deepseek-v4-flash',
    'deepseek/deepseek-v4-pro',
    'siliconflow-cn/Qwen/Qwen3.6-35B-A3B',
    'xai/grok-4.3',
}
missing = sorted(required.difference(ids))
if missing:
    raise SystemExit(f'missing public models: {missing}')
counts = {prefix: sum(1 for mid in ids if isinstance(mid, str) and mid.startswith(prefix)) for prefix in ['ollama/', 'deepseek/', 'siliconflow-cn/', 'xai/']}
if counts['ollama/'] < 20 or counts['deepseek/'] < 2 or counts['siliconflow-cn/'] < 20 or counts['xai/'] < 1:
    raise SystemExit(f'provider catalog counts too low: {counts}')
non_chat_markers = ['embedding', 'reranker', 'image', 'bge', 'kolors', 'cosyvoice', 'sensevoice', 'telespeech', 'wan2.', 'ocr']
non_chat = [mid for mid in ids if any(marker in str(mid).lower() for marker in non_chat_markers)]
if non_chat:
    raise SystemExit(f'non-chat models leaked into chat catalog: {non_chat[:10]}')
print(json.dumps({'catalog_count': len(ids), 'counts': counts, 'sample': ids[:8]}, ensure_ascii=False, indent=2))
PY

echo '--- split-provider surfaces must be absent ---'
for path in /siliconflow-cn/v1/models /siliconflow-cn/v1/chat/completions; do
  status="$(curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:4000${path}")"
  echo "${path}: HTTP ${status}"
  if [[ "$status" != "404" && "$status" != "405" ]]; then
    echo "expected ${path} to be absent from the unified provider surface" >&2
    exit 1
  fi
done

echo '--- /v1/model-capabilities reasoning metadata ---'
curl -fsS http://127.0.0.1:4000/v1/model-capabilities > "$MODELS_TMP"
python3 - "$MODELS_TMP" <<'PY'
import json, sys
payload = json.load(open(sys.argv[1]))
models = payload.get('models') or {}
glm = models.get('ollama/glm-5.1') or {}
reasoning = glm.get('reasoning') or {}
if reasoning.get('enabled') is not True:
    raise SystemExit('ollama/glm-5.1 should expose reasoning.enabled=true')
if not {'low', 'medium', 'high'}.issubset(set(reasoning.get('efforts') or [])):
    raise SystemExit('ollama/glm-5.1 should expose low/medium/high reasoning efforts')
qwen = models.get('siliconflow-cn/Qwen/Qwen3.6-35B-A3B') or {}
qwen_reasoning = qwen.get('reasoning') or {}
if qwen_reasoning.get('enabled') is not False:
    raise SystemExit('siliconflow-cn/Qwen/Qwen3.6-35B-A3B should expose reasoning.enabled=false')
print(json.dumps({
    'capability_count': len(models),
    'reasoning_model': 'ollama/glm-5.1',
    'reasoning_enabled': reasoning.get('enabled'),
    'reasoning_efforts': reasoning.get('efforts'),
}, ensure_ascii=False, indent=2))
PY

echo '--- unsupported metadata endpoints must be absent ---'
for path in /v1/model/info /model/info; do
  status="$(curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:4000${path}")"
  echo "${path}: HTTP ${status}"
  if [[ "$status" != "404" ]]; then
    echo "expected ${path} to be absent" >&2
    exit 1
  fi
done

echo '--- Hermes APISIX ProviderProfile discovery ---'
(cd "$HOME/.hermes/hermes-agent" && HERMES_HOME="$HOME/.hermes" "$HOME/.hermes/hermes-agent/venv/bin/python" - <<'PY'
from hermes_cli.models import provider_model_ids
from providers import get_provider_profile
models = provider_model_ids('apisix')
profile_models = get_provider_profile('apisix').fetch_models(timeout=5)
required = {'ollama/glm-5.1', 'deepseek/deepseek-v4-flash', 'siliconflow-cn/Qwen/Qwen3.6-35B-A3B', 'xai/grok-4.3'}
missing = required.difference(models or [])
print({'provider_model_count': len(models or []), 'profile_fetch_model_count': len(profile_models or []), 'required_present': not missing})
if missing:
    raise SystemExit(f'missing APISIX-discovered models: {sorted(missing)}')
PY
)

check_model() {
  local model="$1" marker="$2"
  TMP="$(mktemp)"
  BODY="$(mktemp)"
  python3 - <<PY > "$BODY"
import json
print(json.dumps({'model': '$model', 'messages':[{'role':'user','content':'Reply with exactly $marker and no other text.'}], 'temperature':0, 'max_tokens':512}))
PY
  local status
  status="$(curl -sS -o "$TMP" -w '%{http_code}' http://127.0.0.1:4000/v1/chat/completions \
    -H 'Content-Type: application/json' --data-binary @"$BODY")"
  python3 - "$TMP" "$model" "$marker" "$status" <<'PY'
import json, sys
path, model, marker, status = sys.argv[1:]
j = json.load(open(path))
choice = (j.get('choices') or [{}])[0]
msg = choice.get('message') or {}
content = (msg.get('content') or '').strip()
summary = {'model': model, 'status': status, 'response_model': j.get('model'), 'finish_reason': choice.get('finish_reason'), 'content': content, 'usage': j.get('usage')}
print(json.dumps(summary, ensure_ascii=False, indent=2))
if status != '200' or content != marker or choice.get('finish_reason') != 'stop':
    raise SystemExit(f'{model} semantic check failed')
PY
  rm -f "$TMP" "$BODY"
}

echo '--- semantic checks across providers ---'
check_model 'ollama/glm-5.1' 'APISIX_OK'
check_model 'deepseek/deepseek-v4-flash' 'DEEPSEEK_OK'
check_model 'siliconflow-cn/Qwen/Qwen3.6-35B-A3B' 'SILICONFLOW_OK'
check_model 'xai/grok-4.3' 'XAI_OK'
