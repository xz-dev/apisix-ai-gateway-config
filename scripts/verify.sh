#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ADMIN_KEY="$(tr -d '\r\n' < "$ROOT/conf/admin.key")"

echo '--- APISIX Admin API model routes ---'
ROUTES_TMP="$(mktemp)"
trap 'rm -f "${ROUTES_TMP:-}" "${TMP:-}" "${TMP2:-}"' EXIT
curl -fsS http://127.0.0.1:9180/apisix/admin/routes \
  -H "X-API-KEY: $ADMIN_KEY" > "$ROUTES_TMP"
python - "$ROUTES_TMP" <<'PY'
import json, sys
payload = json.load(open(sys.argv[1]))
routes = payload.get('list') or []
summary = []
for item in routes:
    value = item.get('value') or item
    plugins = value.get('plugins') or {}
    if 'ai-proxy' in plugins:
        raise SystemExit(f"direct ai-proxy route violates unified pool routing: {value.get('id')}")
    if 'ai-proxy-multi' in plugins or value.get('uri') == '/v1/models':
        summary.append({
            'id': value.get('id'),
            'name': value.get('name'),
            'uri': value.get('uri'),
            'vars': value.get('vars'),
            'plugins': sorted(plugins),
        })
ids = {r['id'] for r in summary}
required = {'pool-ollama-glm-5-1', 'pool-siliconflow-qwen-vision', 'main-models'}
missing = required.difference(ids)
if missing:
    raise SystemExit(f'missing required pool routes: {sorted(missing)}')
for forbidden in {'main-chat', 'vision-chat', 'vision-models'}:
    if forbidden in ids:
        raise SystemExit(f'historical split route still present: {forbidden}')
print(json.dumps(summary, ensure_ascii=False, indent=2))
PY

echo '--- /v1/models ---'
curl -fsS http://127.0.0.1:4000/v1/models | python -m json.tool

echo '--- split-provider surfaces must be absent ---'
for path in /siliconflow-cn/v1/models /siliconflow-cn/v1/chat/completions; do
  status="$(curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:4000${path}")"
  echo "${path}: HTTP ${status}"
  if [[ "$status" != "404" && "$status" != "405" ]]; then
    echo "expected ${path} to be absent from the unified provider surface" >&2
    exit 1
  fi
done

echo '--- LiteLLM-specific metadata shims must be absent ---'
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
print({'provider_model_ids': models, 'profile_fetch_models': profile_models})
required = {'ollama/glm-5.1', 'siliconflow-cn/Qwen/Qwen3.6-35B-A3B'}
missing = required.difference(models or [])
if missing:
    raise SystemExit(f'missing APISIX-discovered models: {sorted(missing)}')
PY
)

echo '--- /v1/chat/completions semantic check: ollama pool ---'
TMP="$(mktemp)"
curl -fsS http://127.0.0.1:4000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"ollama/glm-5.1","messages":[{"role":"user","content":"Reply with exactly APISIX_OK and no other text."}],"temperature":0,"max_tokens":512}' > "$TMP"
python - "$TMP" <<'PY'
import json, sys
j = json.load(open(sys.argv[1]))
choice = (j.get('choices') or [{}])[0]
msg = choice.get('message') or {}
content = (msg.get('content') or '').strip()
summary = {
    'model': j.get('model'),
    'finish_reason': choice.get('finish_reason'),
    'content': content,
    'has_reasoning': bool(msg.get('reasoning') or msg.get('reasoning_content')),
    'usage': j.get('usage'),
}
print(json.dumps(summary, ensure_ascii=False, indent=2))
if content != 'APISIX_OK' or choice.get('finish_reason') != 'stop':
    raise SystemExit('ollama pool semantic check failed')
PY

echo '--- /v1/chat/completions semantic check: vision pool through unified provider ---'
TMP2="$(mktemp)"
curl -fsS http://127.0.0.1:4000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"siliconflow-cn/Qwen/Qwen3.6-35B-A3B","messages":[{"role":"user","content":"Reply with exactly VISION_POOL_OK and no other text."}],"temperature":0,"max_tokens":512}' > "$TMP2"
python - "$TMP2" <<'PY'
import json, sys
j = json.load(open(sys.argv[1]))
choice = (j.get('choices') or [{}])[0]
msg = choice.get('message') or {}
content = (msg.get('content') or '').strip()
summary = {
    'model': j.get('model'),
    'finish_reason': choice.get('finish_reason'),
    'content': content,
    'usage': j.get('usage'),
}
print(json.dumps(summary, ensure_ascii=False, indent=2))
if content != 'VISION_POOL_OK' or choice.get('finish_reason') != 'stop':
    raise SystemExit('vision pool semantic check failed')
PY
