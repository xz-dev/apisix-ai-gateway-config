# Round 2 routing semantics / APISIX config correctness review

## Blocker

None found.

## Fix now

None found.

## Optional

None found for this review angle.

## Defer

None found for this review angle.

## Verified correct

- Direct origin IDs are rendered as `origin/<logical-provider>/<raw-provider-model-id>` and preserve raw model slashes. Evidence: `origin_model_id()` constructs `origin/{provider_id}/{raw_model_id}` and `expand_provider_models()` uses it for every provider catalog model (`scripts/render-routes.py:427-439`). The route matcher is an exact `post_arg.model == <origin id>` comparison (`scripts/render-routes.py:529-548`). Tests assert no legacy `test/model-a` alias and preserved raw slashes (`tests/test_render_routes.py:165-193`).
- `/v1/models` exposes origin IDs plus root IDs. Evidence: `build_catalog()` appends all expanded origin models and all root routes before `models_route()` serializes the catalog (`scripts/render-routes.py:746-749`, `scripts/render-routes.py:753-767`). Tests assert the catalog contains `origin/ollama/deepseek-v4-pro`, `origin/deepseek/deepseek-v4-pro`, and root `deepseek-v4-pro` (`tests/test_render_routes.py:242-259`).
- Provider accounts/deployments are hidden behind logical providers and default to same-priority/equal-weight. Evidence: provider IDs, not account names, are used in public origin IDs (`scripts/render-routes.py:427-439`); default `instance_priority` is `0`, `fallback_instance_priority` defaults to the same value, and `instance_weight` defaults to `1` (`scripts/render-routes.py:280-316`). Contract tests assert multiple Ollama keys render as `ollama-1`, `ollama-2`, `ollama-3` with priority `{0}` and weight `{1}` (`tests/test_gateway_route_contract.py:110-130`).
- Root namespace regex/template rules are expanded at render time into explicit static APISIX routes, not runtime APISIX regex capture. Evidence: `build_root_routes()` matches Python regexes against expanded upstream model IDs during rendering, resolves templates to concrete target IDs, and creates `RootRoute` objects (`scripts/render-routes.py:458-485`); `root_pool_route()` emits an exact `post_arg.model == <root id>` route with static `ai-proxy-multi.instances[].options.model` values (`scripts/render-routes.py:552-583`). The ADR states this is required because APISIX `ai-proxy-multi.options.model` is static (`docs/adr/0001-origin-root-model-routing.md:1-3`).
- Cross-provider fallback is only on root routes, not explicit origin routes. Evidence: origin routes call `instances_for_model()` for a single `ExpandedModel` and therefore only include credentials from that model's logical provider (`scripts/render-routes.py:529-548`). Root routes expand ordered origin targets into combined instances (`scripts/render-routes.py:552-583`). Contract tests assert direct `origin/ollama/deepseek-v4-pro` contains only the Ollama/openai-compatible provider while root `deepseek-v4-pro` contains Ollama then DeepSeek (`tests/test_gateway_route_contract.py:185-225`).
- DeepSeek root rule matches the approved behavior: `conf/model-pools.json` configures `deepseek-*` roots to target `origin/ollama/{model}` first and `origin/deepseek/{model}` second with only `http_429`/`http_5xx` fallback (`conf/model-pools.json:16-31`). The renderer rejects `rate_limiting` and unknown fallback strategies (`scripts/render-routes.py:181-190`) and bounds timeout to 1-60000 ms without claiming timeout fallback (`scripts/render-routes.py:250-268`). Tests assert target order, numeric priority order `[1000, 999]`, and no `rate_limiting` (`tests/test_render_routes.py:196-239`).
- APISIX route IDs are safe, stable, unique, and length-bounded. Evidence: route IDs are slugged to `[A-Za-z0-9_.-]`, include a stable SHA-1 suffix, and are capped at 64 chars (`scripts/render-routes.py:120-147`); write-time checks reject IDs longer than 64 chars or duplicate IDs (`scripts/render-routes.py:789-795`). Tests cover collision and long-model cases (`tests/test_render_routes.py:307-320`).

## Commands run

- `git status --short && git diff --stat && git diff -- CONTEXT.md 'docs/adr*' conf/model-pools.json scripts/render-routes.py tests || true` — exit 0.
- `pytest -q` — exit 127 (`pytest` executable not installed).
- `python -m pytest -q` — exit 1 (`No module named pytest`).
- Manual static-catalog render with Ollama + DeepSeek root rule — exit 0. Verified manifest models were `["deepseek-v4-pro", "origin/deepseek/deepseek-v4-pro", "origin/ollama/deepseek-v4-pro", "origin/ollama/glm-5.1"]`; root route instances were Ollama priority 1000 then DeepSeek priority 999 with `fallback_strategy: ["http_429", "http_5xx"]`; direct origin routes had only their own provider instances.
- Manual multi-account origin render — exit 0. Verified `origin/ollama/glm-5.1` had `ollama-1` and `ollama-2` with priority `0`, weight `1`, and same-provider `http_429`/`http_5xx` fallback.

User approval needed: no; no changes are recommended for this review angle.
