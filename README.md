# Local APISIX AI Gateway Config

Minimal Docker Compose/config repository for running Apache APISIX as a clean local OpenAI-compatible AI gateway on `127.0.0.1:4000`.

This repository is intentionally **not** a fork of `apache/apisix`; it only contains deployment/configuration files. Runtime uses the official `apache/apisix:latest` Docker image.

## What it deploys

- APISIX gateway: `127.0.0.1:4000 -> 9080`
- APISIX Admin API: `127.0.0.1:9180 -> 9180`
- etcd for APISIX config storage
- OpenAI-compatible routes:
  - `GET /v1/models`
  - `GET /v1/model-capabilities`
  - `POST /v1/chat/completions`

## Architecture

All model traffic uses the same pool abstraction. `conf/model-pools.json` is the no-secret registry for public model pools; `scripts/render-routes.py` expands it into explicit APISIX `ai-proxy-multi` routes because APISIX instances use static upstream `options.model`.

A single-backend model is still configured as a one-instance pool. Multi-key and fallback-provider cases use explicit instance `weight`, `priority`, and gateway-level `fallback_strategy` settings. Provider-backed models are selected by public model IDs on the unified `/v1` endpoint; there are no provider-specific client URL prefixes.

Current public model families:

- `ollama/<upstream-model>` through Ollama Cloud, with `OLLAMA_CLOUD_KEY_1` and optional `OLLAMA_CLOUD_KEY_2` as same-priority load-balanced instances.
- `deepseek/<upstream-model>` through the official DeepSeek API.
- `siliconflow-cn/<upstream-model>` through SiliconFlow CN; non-chat catalog entries such as embedding/reranker/image/audio/OCR models are filtered out of the chat catalog.
- `xai/<upstream-model>` through the official xAI API with lower instance priority for fallback-style usage.

Capability metadata is exposed through APISIX's own `GET /v1/model-capabilities` endpoint. Clients can consume reasoning availability, reasoning-effort choices, context windows, and related metadata without adding provider-specific branches.

## Config files

- `conf/config.yaml` — APISIX runtime config and enabled plugin list.
- `conf/model-pools.json` — no-secret model pool registry used by `scripts/render-routes.py`.
- `conf/model-capabilities.json` — fallback capability registry rendered into `GET /v1/model-capabilities`.
- `env.example` — template for provider API keys.
- `conf/admin.key.example` — template for the Admin API key used by scripts.

See `docs/model-pools.md` for the complete `conf/model-pools.json` field reference.

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

`conf/admin.key` must match `deployment.admin.admin_key[0].key` in `conf/config.yaml` unless you intentionally change both.

## Start

```bash
docker compose up -d
./scripts/configure-routes.sh
./scripts/verify.sh
```

## Verify

`./scripts/verify.sh` checks local gateway state:

- APISIX Admin API managed routes
- generated `GET /v1/models` catalog
- generated `GET /v1/model-capabilities` metadata
- absence of direct `ai-proxy` route bypasses
- absence of unsupported metadata endpoints
- absence of provider-specific client URL prefixes

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
