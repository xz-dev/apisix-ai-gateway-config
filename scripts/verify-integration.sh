#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP=""
BODY=""
trap 'rm -f "${TMP:-}" "${BODY:-}"' EXIT

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
  TMP=""
  BODY=""
}

echo '--- semantic checks across providers ---'
check_model 'ollama/glm-5.1' 'APISIX_OK'
check_model 'deepseek/deepseek-v4-flash' 'DEEPSEEK_OK'
check_model 'siliconflow-cn/Qwen/Qwen3.6-35B-A3B' 'SILICONFLOW_OK'
check_model 'xai/grok-4.3' 'XAI_OK'
