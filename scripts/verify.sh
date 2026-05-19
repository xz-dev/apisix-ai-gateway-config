#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ADMIN_KEY="$(tr -d '\r\n' < "$ROOT/conf/admin.key")"

echo '--- APISIX Admin API model routes ---'
ROUTES_TMP="$(mktemp)"
trap 'rm -f "${ROUTES_TMP:-}" "${TMP:-}"' EXIT
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
    if 'ai-proxy' in plugins or 'ai-proxy-multi' in plugins or value.get('uri', '').endswith('/models'):
        summary.append({
            'id': value.get('id'),
            'name': value.get('name'),
            'uri': value.get('uri'),
            'plugins': sorted(plugins),
        })
print(json.dumps(summary, ensure_ascii=False, indent=2))
PY

echo '--- /v1/models ---'
curl -fsS http://127.0.0.1:4000/v1/models | python -m json.tool

echo '--- /siliconflow-cn/v1/models ---'
curl -fsS http://127.0.0.1:4000/siliconflow-cn/v1/models | python -m json.tool

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

echo '--- /v1/chat/completions semantic check ---'
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
    raise SystemExit('chat completion semantic check failed')
PY
