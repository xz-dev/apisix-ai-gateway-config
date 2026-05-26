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
  - `OPTIONS /v1/*` CORS preflight for browser clients

## Architecture

All model traffic uses the same pool abstraction. `conf/model-pools.json` is the no-secret registry for logical providers and root model resolution rules; `scripts/render-routes.py` expands it into explicit APISIX `ai-proxy-multi` routes because APISIX instances use static upstream `options.model`. The generated route set also includes a high-priority `OPTIONS /v1/*` CORS preflight route so browser clients can call the OpenAI-compatible API. `scripts/deploy-routes.py` is the deployment pipeline that renders desired routes, applies them through the APISIX Admin API, and removes stale repo-managed routes.

Direct provider-origin models are exposed as `origin/<provider>/<raw-provider-model-id>`, for example `origin/ollama/glm-5.1` or `origin/siliconflow-cn/Qwen/Qwen3.6-35B-A3B`. Old provider-prefixed IDs such as `ollama/glm-5.1` are not generated. Root model IDs, such as `deepseek-v4-pro`, are provider-neutral aliases generated only by explicit `root_model_rules`; their fallback targets reference canonical `origin/...` IDs.

Provider deployments/accounts like `ollama-1` and `ollama-2` are hidden behind logical provider `ollama` and default to same-priority equal-weight APISIX load balancing. Root DeepSeek models prefer Ollama-hosted DeepSeek origins first and fall back to official DeepSeek origins on APISIX-supported `http_429`/`http_5xx` failures. Timeout fallback is intentionally deferred; upstream timeouts remain bounded.

Current public model families:

- `origin/ollama/<upstream-model>` through Ollama Cloud, with `OLLAMA_CLOUD_KEY_1` and optional additional keys as same-priority load-balanced deployments.
- `origin/deepseek/<upstream-model>` through the official DeepSeek API.
- `origin/siliconflow-cn/<upstream-model>` through SiliconFlow CN; non-chat catalog entries such as embedding/reranker/image/audio/OCR models are filtered out of the chat catalog.
- `origin/xai/<upstream-model>` through the official xAI API.
- Root aliases such as `deepseek-v4-pro` when configured by `root_model_rules`.

Capability metadata is exposed through APISIX's own `GET /v1/model-capabilities` endpoint. Clients can consume reasoning availability, reasoning-effort choices, context windows, and related metadata without adding provider-specific branches. Reasoning metadata is treated as model-centric: if a provider catalog omits reasoning flags, local/model-centric metadata can still describe the underlying model's reasoning support.

## Config files

- `conf/config.yaml` — APISIX runtime config and enabled plugin list.
- `conf/model-pools.json` — no-secret model pool registry used by `scripts/render-routes.py`.
- `conf/model-capabilities.json` — final capability registry rendered into `GET /v1/model-capabilities`. Build it by converting LiteLLM's upstream `model_prices_and_context_window.json` into the APISIX shape, optionally overlaying OpenRouter provider metadata above LiteLLM, and overlaying local APISIX entries/overrides above both. Local/model-centric overrides are important for reasoning support and effort values when provider catalogs omit them.
- `env.example` — template for provider API keys.
- `conf/admin.key.example` — template for the Admin API key used by scripts.

See `docs/model-pools.md` for the complete `conf/model-pools.json` field reference.

To regenerate a merged capability registry from LiteLLM plus the local override file:

```bash
./scripts/build-model-capabilities.py \
  --base conf/model-capabilities.json \
  --output conf/model-capabilities.json
./scripts/configure-routes.sh
```

The raw LiteLLM JSON is an input to this build step, not a file checked into this repository and not a runtime dependency of the Hermes APISIX ProviderProfile.

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
# Numbered variables are expanded into deployments in a round-robin pool.
OLLAMA_CLOUD_KEY_1=replace-me
# OLLAMA_CLOUD_KEY_2=replace-me

# Or use a comma-separated list for arbitrary-size primary pools.
# OLLAMA_CLOUD_KEYS=key-a,key-b,key-c

# Additional keys are same-priority deployments under logical provider `ollama`.
# Cross-provider model fallback is configured by root_model_rules, not by key names.

DEEPSEEK_API_KEY=replace-me
XAI_API_KEY=replace-me
SILICONFLOW_CN_API_KEY=replace-me

# Upstream timeout is configured in conf/model-pools.json router_settings.timeout.
```

`conf/admin.key` must match `deployment.admin.admin_key[0].key` in `conf/config.yaml` unless you intentionally change both.

## Start

```bash
docker compose up -d
./scripts/configure-routes.sh
./scripts/verify.sh
```

## Verify

`./scripts/verify.sh` checks local gateway state only:

- APISIX Admin API managed routes
- generated `GET /v1/models` catalog
- generated `GET /v1/model-capabilities` metadata
- absence of direct `ai-proxy` route bypasses

`./scripts/verify-integration.sh` is intentionally separate. It checks Hermes ProviderProfile discovery and real provider semantic calls, so it requires the Hermes plugin, local Hermes environment, provider API keys, and upstream model availability.

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
  default: origin/ollama/glm-5.1
  base_url: http://127.0.0.1:4000/v1
```
