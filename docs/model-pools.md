# model-pools.json Field Reference

`conf/model-pools.json` is the no-secret source of truth for generated APISIX AI Gateway routes. Version 2 separates direct provider-origin routing from root namespace model resolution so fallback is explicit and testable.

`model-pools.json` does not contain provider API keys. It contains environment variable names. `scripts/configure-routes.sh` loads `.env`, and `scripts/render-routes.py` resolves those names when rendering Admin API route JSON.

## Top-level shape

```json
{
  "version": 2,
  "description": "Managed public model pools for the local APISIX AI gateway.",
  "router_settings": {},
  "root_model_rules": [],
  "providers": []
}
```

## Top-level fields

| Field | Type | Required | Purpose |
| --- | --- | --- | --- |
| `version` | integer | yes | Registry format version. Current value is `2`. |
| `description` | string | no | Human-readable explanation of the registry. |
| `router_settings` | object | yes | Defaults applied to generated `ai-proxy-multi` routes. |
| `root_model_rules` | array | no | Render-time root namespace model rules. These generate provider-neutral model IDs such as `deepseek-v4-pro`. |
| `providers` | array | yes | Logical provider definitions. Each provider expands catalog entries into direct `origin/<provider>/<raw-model-id>` routes. |

## Model ID namespaces

### Origin Model IDs

Every provider catalog model is exposed directly as:

```text
origin/<logical-provider>/<raw-provider-model-id>
```

Examples:

```text
origin/ollama/glm-5.1
origin/deepseek/deepseek-v4-pro
origin/siliconflow-cn/Qwen/Qwen3.6-35B-A3B
```

The raw provider model ID begins after the provider segment and may contain `/` characters. Old provider-prefixed IDs such as `ollama/glm-5.1` are **not** generated as compatibility aliases.

### Root Model IDs

Any generated model ID that does not start with `origin/` is a root model ID. Root IDs are provider-neutral names such as `deepseek-v4-pro`; they are generated only by explicit `root_model_rules`. A root route may fall back across approved origin targets, but an explicit `origin/...` request is provider-pinned and never crosses providers.

## router_settings

These fields become shared `plugins.ai-proxy-multi` settings.

| Field | Type | Current default | Purpose |
| --- | --- | --- | --- |
| `algorithm` | string | `roundrobin` | Load-balancer algorithm for instances with the same priority. Valid values: `roundrobin`, `chash`. |
| `fallback_strategy` | array of strings | `["http_429", "http_5xx"]` | Same-provider account fallback conditions for routes with more than one instance. `rate_limiting` is intentionally not emitted unless real `ai-rate-limiting` config is added later. |
| `timeout` | integer | `30000` | Upstream request timeout in milliseconds. Timeout fallback is deferred because APISIX 3.15 `ai-proxy-multi` documents HTTP 429/5xx fallback, not a `timeout` fallback strategy. |
| `keepalive` | boolean | `true` | Whether to reuse upstream connections. |
| `keepalive_timeout` | integer | `60000` | Upstream keepalive timeout in milliseconds. |
| `keepalive_pool` | integer | `30` | Upstream keepalive connection pool size. |
| `ssl_verify` | boolean | `true` | Whether APISIX verifies upstream TLS certificates. |

Example:

```json
"router_settings": {
  "algorithm": "roundrobin",
  "fallback_strategy": ["http_429", "http_5xx"],
  "timeout": 30000,
  "keepalive": true,
  "keepalive_timeout": 60000,
  "keepalive_pool": 30,
  "ssl_verify": true
}
```

## providers[]

Each provider entry describes one logical provider/catalog and how to expose its raw models as origin routes.

| Field | Type | Required | Purpose |
| --- | --- | --- | --- |
| `id` | string | yes | Stable logical provider identifier. Example: `ollama`, `deepseek`, `siliconflow-cn`, `xai`. |
| `owned_by` | string | yes | Value used in generated `/v1/models` entries for origin IDs. |
| `upstream_prefix` | string | no | Prefix prepended to raw provider model IDs before sending to the provider. Keep explicit as `""` when upstream uses raw catalog IDs. |
| `catalog_url` | string | no | Provider model catalog endpoint. If absent, `catalog_fallback_models` is used as the static catalog. |
| `catalog_fallback_models` | array of strings | yes | Static provider model IDs used when **no** `catalog_url` is configured. Optionally also used for an explicit degraded-catalog mode if `allow_catalog_fallback` is enabled. They are **not** runtime model fallback policy. |
| `allow_catalog_fallback` | boolean | no | Explicit opt-in for using `catalog_fallback_models` after a live catalog fetch failure. Defaults to `false` so failed renders abort and keep the last-good deployed route set. |
| `chat_endpoint` | string | yes | Provider chat completions endpoint used as `instances[].override.endpoint`. |
| `driver` | string | yes | APISIX AI provider driver. Examples: `openai-compatible`, `deepseek`. |
| `env_vars` | array of strings | yes | Ordered API key environment variable names. Each present env var creates one provider deployment. |
| `env_var_prefixes` | array of strings | no | Prefixes used for numbered variables (`PREFIX_1`) and comma-separated lists (`PREFIXS`). |
| `required_env_vars` | array of strings | yes | Env vars that must be present before rendering. Use this for minimum viable provider availability. |
| `instance_priority` | integer | yes | APISIX instance priority for deployments under this provider. Higher numeric priority wins. In v2, provider deployments default to the same priority for equal load balancing. |
| `instance_weight` | integer | yes | Weight assigned to instances with the same priority. Used by the configured load-balancer algorithm. |
| `include_model_patterns` | array of regex strings | no | Optional allowlist. If present, only catalog model IDs matching at least one regex are exposed. |
| `exclude_model_patterns` | array of regex strings | no | Optional denylist. Any catalog model ID matching a regex is filtered out. |
| `route_priority` | integer | yes | APISIX route priority for generated origin chat routes. |

## root_model_rules[]

Root model rules are evaluated at render time against raw provider model IDs from generated origin routes. They produce explicit APISIX routes; APISIX does not dynamically regex-capture model names at request time.

| Field | Type | Required | Purpose |
| --- | --- | --- | --- |
| `id` | string | no | Stable rule identifier used in route labels. |
| `match_regex` | string | yes | Python regex matched against raw provider model IDs. Use named capture `(?P<model>...)` or rely on the first capture group/whole match as `{model}`. |
| `model_template` | string | no | Template for the root model ID. Defaults to `{model}`. The result must not start with `origin/`. |
| `target_templates` | array of strings | yes | Ordered origin model templates such as `origin/ollama/{model}`. Missing targets are skipped; no arbitrary targets are invented. |
| `fallback_strategy` | array of strings | no | APISIX-supported runtime fallback conditions for the generated root route, currently `http_429` and `http_5xx`. Emitted only when the route has more than one instance. |
| `route_priority` | integer | no | APISIX route priority for generated root chat routes. Default `500`. |
| `owned_by` | string | no | Value used in `/v1/models` for root IDs. Default `apisix-root`. |

DeepSeek root rule example:

```json
{
  "id": "deepseek-series-ollama-official",
  "match_regex": "^(?P<model>deepseek-.+)$",
  "model_template": "{model}",
  "target_templates": [
    "origin/ollama/{model}",
    "origin/deepseek/{model}"
  ],
  "fallback_strategy": ["http_429", "http_5xx"],
  "route_priority": 500,
  "owned_by": "apisix-root"
}
```

For `deepseek-v4-pro`, this renders a root route that tries `origin/ollama/deepseek-v4-pro` first and then `origin/deepseek/deepseek-v4-pro` on APISIX-supported 429/5xx failures. The direct route `origin/ollama/deepseek-v4-pro` remains provider-pinned.

## Generated route behavior

For every exposed origin or root model, `scripts/render-routes.py` emits one `POST /v1/chat/completions` route:

```json
{
  "uri": "/v1/chat/completions",
  "methods": ["POST"],
  "vars": [["post_arg.model", "==", "origin/ollama/glm-5.1"]],
  "plugins": {
    "ai-proxy-multi": {
      "instances": [],
      "balancer": {"algorithm": "roundrobin"},
      "timeout": 30000
    }
  }
}
```

Important invariants:

- Every model request goes through `ai-proxy-multi`.
- Model selection is based on exact `post_arg.model == <public_model_id>` matching.
- Origin routes include only deployments from their logical provider.
- Root routes expand ordered origin targets into APISIX instance priority tiers; earlier targets get higher numeric priority.
- A model with one upstream key is still represented as a one-instance pool, but `fallback_strategy` is emitted only when there is another instance to try.
- Provider API keys are not committed; rendered routes receive `Authorization: Bearer <token>` from `.env` at configure time.
- `GET /v1/models` is generated from the same origin + root catalog.
- `GET /v1/model-capabilities` maps origin IDs from provider-prefixed, raw-model, or explicit origin metadata where available. Root IDs use the first useful capability metadata in target order, preferring entries with reasoning support and reasoning effort values.

## Capability metadata and reasoning

Reasoning support is model-centric. Providers such as Ollama or SiliconFlow may omit reasoning flags even when the underlying model supports reasoning. The renderer therefore does not treat absent provider-origin metadata as proof that reasoning is unsupported; it can reuse explicit local/model-centric metadata such as `deepseek/deepseek-v4-pro` or raw `deepseek-v4-pro` for matching origin/root IDs.

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
python3 -m py_compile scripts/render-routes.py
./scripts/configure-routes.sh
./scripts/verify.sh
```

To check that the rendered catalog matches the registry intent:

```bash
curl -fsS http://127.0.0.1:4000/v1/models
curl -fsS http://127.0.0.1:4000/v1/model-capabilities
```
