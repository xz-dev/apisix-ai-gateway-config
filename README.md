# Local APISIX AI Gateway Config

Minimal Docker Compose/config repository for running Apache APISIX as a clean local OpenAI-compatible AI gateway on `127.0.0.1:4000`.

This repository is intentionally **not** a fork of `apache/apisix`; it only contains deployment/configuration files. Runtime uses the official `apache/apisix:latest` Docker image.

## What it deploys

- APISIX gateway: `127.0.0.1:4000 -> 9080`
- APISIX Admin API: `127.0.0.1:9180 -> 9180`
- etcd for APISIX config storage
- OpenAI-compatible routes:
  - `GET /v1/models`
  - `POST /v1/chat/completions`

All model traffic uses the same pool abstraction. `conf/model-pools.json` is the no-secret registry for migrated provider surfaces; `scripts/render-routes.py` expands it into explicit APISIX `ai-proxy-multi` routes because APISIX instances use static upstream `options.model`. A single-backend model is still configured as a one-instance pool, and multi-key/provider cases use same-priority round-robin plus 429/5xx retry fallback. There is no separate SiliconFlow provider surface; SiliconFlow-backed models are selected by their public model IDs on the unified `/v1` endpoint.

Migrated LiteLLM surfaces:

- `ollama/*` -> `ollama/<upstream-model>` through Ollama Cloud, with `OLLAMA_CLOUD_KEY_1` and optional `OLLAMA_CLOUD_KEY_2` as same-priority load-balanced instances.
- `deepseek/*` -> `deepseek/<upstream-model>` through the official DeepSeek API.
- `siliconflow-cn/*` -> `siliconflow-cn/<upstream-model>` through SiliconFlow CN; non-chat catalog entries such as embedding/reranker/image/audio models are filtered out of the chat catalog.
- global `* -> xai/*` fallback -> explicit `xai/<upstream-model>` routes through the official xAI API with lower instance priority.

This is not a LiteLLM compatibility layer. LiteLLM-specific endpoints `/v1/model/info` and `/model/info` should remain absent and return 404.

## Secret files

Do not commit these files:

```text
.env
conf/admin.key
```

Create them from examples:

```bash
cp env.example .env
cp conf/admin.key.example conf/admin.key
chmod 600 .env conf/admin.key
```

Edit `.env`:

```bash
OLLAMA_CLOUD_KEY_1=replace-me
OLLAMA_CLOUD_KEY_2=replace-me   # optional but recommended: same-priority LB/fallback
DEEPSEEK_API_KEY=replace-me
XAI_API_KEY=replace-me
SILICONFLOW_CN_API_KEY=replace-me
```

`configure-routes.sh` also sources the historical LiteLLM env file at `~/.config/litellm/litellm.env` first during migration, then overlays APISIX-local `.env`. Keep `.env` authoritative after migration.

`conf/admin.key` must match `deployment.admin.admin_key[0].key` in `conf/config.yaml` unless you intentionally change both.

## Start

```bash
docker compose up -d
./scripts/configure-routes.sh
./scripts/verify.sh
```

## Hermes ProviderProfile plugin

Use the separate plugin repo:

```text
https://github.com/xz-dev/hermes-apisix-provider
```

Install it under:

```bash
mkdir -p ~/.hermes/plugins/model-providers
cp -a /path/to/hermes-apisix-provider/apisix ~/.hermes/plugins/model-providers/apisix
```

Then start a new Hermes process and configure:

```yaml
model:
  provider: apisix
  default: ollama/glm-5.1
  base_url: http://127.0.0.1:4000/v1
```
