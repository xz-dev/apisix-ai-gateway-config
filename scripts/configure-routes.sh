#!/usr/bin/env bash
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
APISIX_ENV="$ROOT/.env"

if [ ! -f "$APISIX_ENV" ]; then
  echo "missing $APISIX_ENV" >&2
  echo "Create it with provider API keys such as OLLAMA_CLOUD_KEY_1 or OLLAMA_CLOUD_KEYS, plus DEEPSEEK_API_KEY, XAI_API_KEY, and SILICONFLOW_CN_API_KEY." >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
. "$APISIX_ENV"
set +a

catalog_timeout=20.0
deploy_args=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --catalog-timeout)
      if [ "$#" -lt 2 ] || [ -z "$2" ]; then
        echo "--catalog-timeout requires a numeric argument" >&2
        exit 1
      fi
      catalog_timeout="$2"
      shift 2
      ;;
    --catalog-timeout=*)
      catalog_timeout="${1#*=}"
      shift
      ;;
    *)
      deploy_args+=("$1")
      shift
      ;;
  esac
done

extract_public_catalog() {
  python3 - "$1" "$2" <<'PY'
import json
import pathlib
import sys

route_path = pathlib.Path(sys.argv[1])
output_path = pathlib.Path(sys.argv[2])
route = json.loads(route_path.read_text(encoding="utf-8"))
response_example = (((route.get("plugins") or {}).get("mocking") or {}).get("response_example"))
if not isinstance(response_example, str):
    raise SystemExit("failed to extract temporary catalog: route-main-models missing mocking.response_example")
payload = json.loads(response_example)
data = payload.get("data")
if not isinstance(data, list):
    raise SystemExit("failed to extract temporary catalog: expected data[]")
output_path.write_text(json.dumps({"data": data}, ensure_ascii=False), encoding="utf-8")
PY
}

deploy_routes() {
  local capabilities_path=$1
  shift
  python3 "$ROOT/scripts/deploy-routes.py" \
    --registry "$ROOT/conf/model-pools.json" \
    --capabilities "$capabilities_path" \
    --admin-key-file "$ROOT/conf/admin.key" \
    --admin-url "http://127.0.0.1:9180" \
    "$@"
}

render_routes() {
  local out_dir=$1
  local manifest_path=$2
  local capabilities_path=$3
  python3 "$ROOT/scripts/render-routes.py" \
    --registry "$ROOT/conf/model-pools.json" \
    --capabilities "$capabilities_path" \
    --out-dir "$out_dir" \
    --manifest "$manifest_path" \
    --catalog-timeout "$catalog_timeout"
}

build_capabilities() {
  local output_path=$1
  local public_catalog=$2
  python3 "$ROOT/scripts/build-model-capabilities.py" \
    --base "$ROOT/conf/model-capabilities.json" \
    --output "$output_path" \
    --openrouter "https://openrouter.ai/api/v1/models" \
    --public-catalog "$public_catalog"
}

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

preflight_out="$tmpdir/preflight"
preflight_manifest="$preflight_out/manifest.json"
render_routes "$preflight_out" "$preflight_manifest" "$ROOT/conf/model-capabilities.json"

model_catalog="$tmpdir/public-catalog.json"
extract_public_catalog "$preflight_out/route-main-models.json" "$model_catalog"

desired_capabilities="$tmpdir/model-capabilities.json"
build_capabilities "$desired_capabilities" "$model_catalog"

render_routes "$preflight_out" "$preflight_manifest" "$desired_capabilities"
deploy_routes "$desired_capabilities" "${deploy_args[@]}"
