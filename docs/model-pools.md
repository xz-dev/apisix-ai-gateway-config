# model-pools.json Field Reference

`conf/model-pools.json` is the no-secret source of truth for generated APISIX model pool routes. It is intentionally explicit: fields with defaults are kept in the checked-in example so the file also works as configuration reference.

`model-pools.json` does not contain provider API keys. It contains environment variable names. `scripts/configure-routes.sh` loads `.env`, and `scripts/render-routes.py` resolves those names when rendering Admin API route JSON.

## Top-level shape

```json
{
  "version": 1,
  "description": "Managed public model pools for the local APISIX AI gateway.",
  "router_settings": {},
  "providers": []
}
```

## Top-level fields

| Field | Type | Required | Purpose |
| --- | --- | --- | --- |
| `version` | integer | yes | Registry format version. Current value is `1`. |
| `description` | string | no | Human-readable explanation of the registry. |
| `router_settings` | object | yes | Defaults applied to every generated `ai-proxy-multi` route. |
| `providers` | array | yes | Provider catalog definitions. Each provider expands into one public route per exposed model. |

## router_settings

These fields become the shared `plugins.ai-proxy-multi` settings for generated model routes.

| Field | Type | Current default | Purpose |
| --- | --- | --- | --- |
| `algorithm` | string | `roundrobin` | Load-balancer algorithm for instances with the same priority. |
| `fallback_strategy` | array of strings | `["http_429", "http_5xx"]` | Conditions that allow APISIX to retry/fall back to another instance. |
| `timeout` | integer | `600000` | Upstream request timeout in milliseconds. Long LLM calls need a larger timeout than normal APIs. |
| `keepalive` | boolean | `true` | Whether to reuse upstream connections. |
| `keepalive_timeout` | integer | `60000` | Upstream keepalive timeout in milliseconds. |
| `keepalive_pool` | integer | `30` | Upstream keepalive connection pool size. |
| `ssl_verify` | boolean | `true` | Whether APISIX verifies upstream TLS certificates. |

Example:

```json
"router_settings": {
  "algorithm": "roundrobin",
  "fallback_strategy": ["http_429", "http_5xx"],
  "timeout": 600000,
  "keepalive": true,
  "keepalive_timeout": 60000,
  "keepalive_pool": 30,
  "ssl_verify": true
}
```

## providers[]

Each provider entry describes one upstream provider/catalog and how to expose its models as public APISIX model IDs.

| Field | Type | Required | Purpose |
| --- | --- | --- | --- |
| `id` | string | yes | Stable provider identifier used in route labels and instance names. Example: `ollama`, `deepseek`, `siliconflow-cn`, `xai`. |
| `owned_by` | string | yes | Value used in generated `/v1/models` entries. This is client-facing catalog metadata only. |
| `public_prefix` | string | yes | Prefix prepended to upstream model IDs to form public model IDs. Example: `ollama/` + `glm-5.1` -> `ollama/glm-5.1`. |
| `upstream_prefix` | string | no | Prefix prepended to upstream model IDs before sending to the provider. Keep explicit as `""` when upstream uses raw catalog IDs. |
| `catalog_url` | string | no | Provider model catalog endpoint. If absent, `fallback_models` is used. If present and fetch fails, fallback is allowed only when `allow_catalog_fallback` is explicitly `true`. |
| `allow_catalog_fallback` | boolean | no | Explicit opt-in for using `fallback_models` after a catalog fetch failure. Defaults to `false` so catalog failures fail fast. |
| `chat_endpoint` | string | yes | Provider chat completions endpoint used as `instances[].override.endpoint`. |
| `driver` | string | yes | APISIX AI provider driver for generated instances. Examples: `openai-compatible`, `deepseek`. |
| `env_vars` | array of strings | yes | Ordered API key environment variable names. Each present env var creates one upstream instance. Multiple values create load-balanced/fallback-capable instances. |
| `required_env_vars` | array of strings | yes | Env vars that must be present before rendering. Use this for minimum viable provider availability. |
| `instance_priority` | integer | yes | Priority assigned to generated instances. Lower priority is preferred by APISIX; higher values are fallback tiers. |
| `instance_weight` | integer | yes | Weight assigned to generated instances with the same priority. Used by the configured load-balancer algorithm. |
| `fallback_models` | array of strings | yes | Static model IDs used when `catalog_url` is absent or cannot be fetched. Also documents must-have models. |
| `include_model_patterns` | array of regex strings | no | Optional allowlist. If present, only catalog model IDs matching at least one regex are exposed. |
| `exclude_model_patterns` | array of regex strings | no | Optional denylist. Any catalog model ID matching a regex is filtered out. Useful for removing embeddings, rerankers, image, audio, video, OCR, and other non-chat models. |
| `route_priority` | integer | yes | APISIX route priority for generated chat routes. Kept explicit so route matching behavior is visible and tunable. |

## Generated route behavior

For every exposed model, `scripts/render-routes.py` emits one `POST /v1/chat/completions` route:

```json
{
  "uri": "/v1/chat/completions",
  "methods": ["POST"],
  "priority": 200,
  "vars": [["post_arg.model", "==", "ollama/glm-5.1"]],
  "plugins": {
    "ai-proxy-multi": {
      "instances": [],
      "balancer": {"algorithm": "roundrobin"},
      "fallback_strategy": ["http_429", "http_5xx"]
    }
  }
}
```

Important invariants:

- Every model request goes through `ai-proxy-multi`.
- A model with one upstream key is still represented as a one-instance pool.
- Model selection is based on exact `post_arg.model == <public_model_id>` matching.
- Provider API keys are not committed; rendered routes receive `Authorization: Bearer <token>` from `.env` at configure time.
- `GET /v1/models` is generated from the same public model catalog.
- `GET /v1/model-capabilities` is generated from the final `conf/model-capabilities.json`, filtered to public models present in the generated catalog. Build that file by converting LiteLLM's upstream `model_prices_and_context_window.json` into APISIX capability shape and overlaying local APISIX entries/overrides. Ollama Cloud reasoning/tools/vision/context metadata should still be queried from native `/api/show` instead.

## Example provider entries

### Same-priority multi-key load balancing

```json
{
  "id": "ollama",
  "owned_by": "ollama-cloud",
  "public_prefix": "ollama/",
  "upstream_prefix": "",
  "catalog_url": "https://ollama.com/v1/models",
  "chat_endpoint": "https://ollama.com/v1/chat/completions",
  "driver": "openai-compatible",
  "env_vars": ["OLLAMA_CLOUD_KEY_1", "OLLAMA_CLOUD_KEY_2"],
  "required_env_vars": ["OLLAMA_CLOUD_KEY_1"],
  "instance_priority": 0,
  "instance_weight": 1,
  "fallback_models": ["glm-5.1"],
  "allow_catalog_fallback": true,
  "route_priority": 200
}
```

If both keys are present, the renderer creates two instances with priority `0` and weight `1`, so requests can be distributed by `router_settings.algorithm`.

### Lower-priority fallback provider

```json
{
  "id": "xai",
  "owned_by": "xai-official",
  "public_prefix": "xai/",
  "upstream_prefix": "",
  "catalog_url": "https://api.x.ai/v1/models",
  "chat_endpoint": "https://api.x.ai/v1/chat/completions",
  "driver": "openai-compatible",
  "env_vars": ["XAI_API_KEY"],
  "required_env_vars": ["XAI_API_KEY"],
  "instance_priority": 10,
  "instance_weight": 1,
  "fallback_models": ["grok-4.3"],
  "allow_catalog_fallback": true,
  "exclude_model_patterns": ["(?i)imagine|image|video"],
  "route_priority": 100
}
```

`instance_priority: 10` makes these instances lower priority than priority `0` instances if combined into a cross-provider pool in future configurations. In the current registry each public model has its own route and pool, but the field is kept explicit for reference and future extension.

## Filtering catalog models

Use `exclude_model_patterns` to keep `/v1/models` chat-focused. For example, SiliconFlow CN has many non-chat model types, so the registry filters common non-chat markers:

```json
"exclude_model_patterns": [
  "(?i)(^|/|-)image($|-)",
  "(?i)embedding",
  "(?i)reranker",
  "(?i)bge",
  "(?i)tts|cosyvoice|sensevoice|telespeech|moss-ttsd",
  "(?i)wan2\\.",
  "(?i)ocr"
]
```

Regexes are Python regular expressions. `(?i)` makes a pattern case-insensitive.

## Validation commands

```bash
cd /home/xz/apisix
python3 -m py_compile scripts/render-routes.py
./scripts/configure-routes.sh
./scripts/verify.sh
```

To check that the rendered catalog matches the registry intent:

```bash
curl -fsS http://127.0.0.1:4000/v1/models
curl -fsS http://127.0.0.1:4000/v1/model-capabilities
```
