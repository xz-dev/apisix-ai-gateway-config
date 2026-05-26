# Review round 1 — determinism, tests, route/fallback/load-balancing validation

Scope notes:
- `/root/apisix-ai-gateway-config/plan.md` and `/root/apisix-ai-gateway-config/progress.md` were not present, so this review is based on the repository state and current files only.
- `git status --short` showed only untracked `.pi/`; no tracked source diff was present.
- `python3 -m pytest -q` could not run because `pytest` is not installed. `python3 -m py_compile scripts/*.py tests/*.py` passed.

Verified non-findings:
- The APISIX deployment mode is standard traditional/etcd + Admin API config: `conf/config.yaml:26-29` and `docker-compose.yml:2-35` match APISIX's traditional etcd/Admin API mode, not standalone YAML mode.
- Generated model routes use the APISIX `ai-proxy-multi` contract shape: `balancer.algorithm`, per-instance `weight`/`priority`, and `fallback_strategy` are emitted in `scripts/render-routes.py:312-355`. This aligns with APISIX ai-proxy-multi docs (`balancer.algorithm` = `roundrobin`/`chash`; instances use `weight` and `priority`; fallback is controlled by `fallback_strategy`).
- There is no current test that relies on random weighted load balancing. Existing tests mostly assert generated route JSON; the issue is missing observable APISIX behavior coverage, not random assertions.

## Blocker

### 1. Generated APISIX route IDs can collide silently and can exceed APISIX Admin API ID limits

Evidence:
- `slug()` replaces every non-alphanumeric run with `-` and only adds a hash when the normalized slug is longer than 80 chars: `scripts/render-routes.py:94-99`.
- Route IDs are generated as `"pool-" + slug(public_model)`: `scripts/render-routes.py:304-308`.
- `write_routes()` does not detect duplicate IDs; it appends them to the manifest and writes `route-<id>.json`, so later routes overwrite earlier files with the same ID: `scripts/render-routes.py:455-461`.
- Deployment then PUTs routes by that ID: `scripts/deploy-routes.py:83-85` and `scripts/deploy-routes.py:121-124`.
- Tests cover duplicate public model IDs (`tests/test_render_routes.py:92-99`) but not normalized route-ID collisions or APISIX ID length.

APISIX doc reference:
- APISIX Admin API route docs define routes as `/apisix/admin/routes/{id}` resources; `PUT /apisix/admin/routes/{id}` creates the specified route. The same docs state string IDs must be 1-64 chars and only contain letters, numbers, dashes, periods, and underscores: <https://apisix.apache.org/docs/apisix/admin-api/#route>.

Why this impacts determinism/testability:
- Different public model IDs such as `test/a/b` and `test/a-b` normalize to the same `pool-test-a-b`. I reproduced this with a temporary registry: the manifest had two `pool-test-a-b` entries and `model_count: 2`, but only one route file existed; one advertised model would never match a route.
- Because catalogs are fetched live, a provider can introduce a colliding or too-long model ID later, causing deploy-time route loss or Admin API rejection that looks like flaky routing.
- Current max length is 85 (`pool-` + 80-char slug), which can violate APISIX's 64-char route ID limit.

Smallest conforming fix / validation:
- Generate IDs with a bounded hash inside the APISIX 64-char limit, e.g. `pool-<truncated-normalized-prefix>-<10-char-sha1>` where total length <= 64, or fail fast if any generated ID collides.
- Add renderer tests for: (1) `a/b` vs `a-b` produce distinct route IDs or fail explicitly, and (2) every generated route ID is <= 64 chars and APISIX-ID-safe.

## Fix now

### 2. `apache/apisix:latest` plus body-based `post_arg.model` route matching is an under-validated APISIX contract dependency

Evidence:
- Model routing depends on the route var `[["post_arg.model", "==", model_id]]`: `scripts/render-routes.py:354`.
- Tests assert that JSON exists in rendered config (`tests/test_gateway_route_contract.py:175-192`) but do not prove the running APISIX image matches JSON request bodies with `post_arg.model`.
- Docker uses the moving tag `apache/apisix:latest`: `docker-compose.yml:22-24`.

APISIX doc/source reference:
- APISIX route docs document route filtering generally and document POST form matching as `post_arg_name` for `application/x-www-form-urlencoded`: <https://apisix.apache.org/docs/apisix/router-radixtree/>.
- Newer official APISIX source supports `post_arg.` JSON/body lookup in `apisix/core/ctx.lua`, but this is not validated here against the exact container tag being run.

Why this impacts determinism/testability:
- The renderer can pass all unit tests while real OpenAI-compatible JSON requests return `404 Route Not Found` if the APISIX image does not support or changes `post_arg.model` behavior.
- Because the image tag floats, behavior can change without a repo diff.

Smallest conforming fix / validation:
- Pin APISIX to a verified version instead of `latest`.
- Add an APISIX-level contract test that deploys a minimal route with `vars: [["post_arg.model", "==", "test/model"]]`, sends `Content-Type: application/json` to `/v1/chat/completions`, and asserts the route is selected via response marker or access log.
- Unapproved architecture decision: replacing unified `/v1/chat/completions` body-based routing with provider/model-specific paths would change the public API and should not be done without product approval.

### 3. `verify-gateway.py` requires optional secrets and live catalog sizes, so a valid minimal deployment can fail verification

Evidence:
- README/env example describe the second Ollama key as optional: `README.md:76-84` and `env.example:1-9`.
- `check_instance_priorities()` requires `ollama/glm-5.1` to have at least two instances and priority `{100}`: `scripts/verify-gateway.py:173-185`.
- `check_public_catalog()` requires live provider catalog counts (`ollama/ >= 20`, `siliconflow-cn/ >= 20`, etc.): `scripts/verify-gateway.py:218-229`.

APISIX doc reference:
- APISIX ai-proxy-multi load balancing is controlled by configured instances, weights, priorities, and `balancer.algorithm`; the docs do not require multiple credentials for a valid pool: <https://apisix.apache.org/docs/apisix/plugins/ai-proxy-multi/>.

Why this impacts determinism/testability:
- A one-key Ollama deployment is valid per repo docs and renderer invariants, but `verify.sh` fails it.
- Live catalog counts can change due provider-side catalog changes or fallback mode, producing failures unrelated to route correctness.
- This conflates baseline gateway validation with a load-balancing scenario that needs controlled test inputs.

Smallest conforming fix / validation:
- Baseline verification should assert: each public model route has >=1 instance, required fallback models exist, route catalog and Admin API labels are consistent, and configured `fallback_strategy`/timeout are present.
- Move multi-key load-balancing assertions into a separate deterministic test that creates known mock instances or reads an explicit expected instance count from test setup.

### 4. Live catalog fallback can silently replace the deployed route set with a much smaller fallback set

Evidence:
- All current providers have `allow_catalog_fallback: true`: `conf/model-pools.json:23-41`, `conf/model-pools.json:48-64`, `conf/model-pools.json:71-103`, and `conf/model-pools.json:110-128`.
- On catalog fetch error, the renderer falls back to `fallback_models` and continues: `scripts/render-routes.py:283-293`.
- `configure-routes.sh` deploys rendered routes, refreshes capabilities from live OpenRouter + live `/v1/models`, and deploys again: `scripts/configure-routes.sh:27-37`.

APISIX doc reference:
- APISIX Admin API applies the route set exactly as submitted via route IDs; this fallback behavior is repository logic, not APISIX load-balancer fallback: <https://apisix.apache.org/docs/apisix/admin-api/#route>.

Why this impacts determinism/testability:
- A transient provider catalog failure can reduce the public route set to fallback models and delete previously managed routes as stale on deploy. That looks like unstable model routing even though APISIX is applying deterministic config.
- `verify-gateway.py` then fails catalog count checks, but the failure points at public catalog size rather than the degraded-catalog decision.

Smallest conforming fix / validation:
- Emit explicit manifest metadata per provider: catalog source (`live` vs `fallback`), error, and model count.
- Either fail deploy on live catalog failure by default, or require an explicit `--allow-degraded-catalog` flag for fallback deployment.
- Unapproved product decision: fail-open vs fail-fast for public model catalog availability changes user-visible model availability; decide before changing production behavior.

### 5. There is no observable APISIX fallback/load-balancing test; current tests stop at generated config or one real-provider semantic request

Evidence:
- Unit tests assert generated instances, weights, priorities, timeout, and exact vars: `tests/test_gateway_route_contract.py:106-181`.
- `verify-gateway.py` checks Admin API route config and CORS, but not selected upstream instance or fallback result: `scripts/verify-gateway.py:120-190` and `scripts/verify-gateway.py:279-285`.
- `verify-integration.sh` sends one semantic request per provider, but does not force 429/5xx/rate-limit fallback or assert round-robin distribution: `scripts/verify-integration.sh:51-55`.

APISIX doc reference:
- APISIX ai-proxy-multi docs define `fallback_strategy` values (`rate_limiting`, `http_429`, `http_5xx`) and `roundrobin` weighted load balancing under `balancer.algorithm`: <https://apisix.apache.org/docs/apisix/plugins/ai-proxy-multi/>.

Why this impacts determinism/testability:
- The most important behavior can regress while all repo tests still pass: route config may look right, but APISIX may not select the expected instance, may not fall back on 429/5xx, or may not expose useful logs.
- Real provider quota exhaustion is not a deterministic test fixture.

Smallest conforming fix / validation:
- Add a deterministic APISIX integration fixture with local OpenAI-compatible mock upstreams, not real provider keys.
- Validate at the APISIX contract boundary: Admin API route config + HTTP response/logs.
- Suggested focused tests after any future fix:
  1. JSON body route match: `model=test/model-a` hits only the route with `post_arg.model == test/model-a`.
  2. Same-priority `roundrobin`: two mock instances with weight 1 return distinct markers; send sequential requests to a fresh route and assert both markers/counts.
  3. 429/5xx fallback: primary returns 429 or 500, fallback returns 200 marker; assert final client response marker and status.
  4. Rate-limit fallback: configure `ai-rate-limiting` on the high-priority instance and assert lower-priority instance is used after quota is exhausted.
  5. Unknown model: assert deterministic 404/route-not-found behavior and logs.

### 6. Project docs invert APISIX priority semantics and conflict with current config/tests

Evidence:
- `docs/model-pools.md:71` says lower priority is preferred and higher values are fallback tiers.
- `docs/model-pools.md:154` says `instance_priority: 10` is lower priority than priority `0`.
- Current config/tests use the opposite for Ollama fallback: primary `100`, fallback `0` in `conf/model-pools.json:34-35`, and tests assert `[100, 100, 0, 0, 0]` in `tests/test_gateway_route_contract.py:129-150`.

APISIX doc reference:
- The ai-proxy-multi docs' priority/rate-limiting example describes the instance with `priority: 1` as the higher-priority instance and `priority: 0` as the lower-priority fallback instance: <https://apisix.apache.org/docs/apisix/plugins/ai-proxy-multi/>.

Why this impacts determinism/testability:
- The code appears to conform to APISIX's numeric priority direction, but the repo docs invite a future change that would invert primary/fallback order and make fallback look unstable or preferred.

Smallest conforming fix / validation:
- Update repo docs to state that higher numeric `priority` is preferred by APISIX; lower numeric priority is fallback when fallback_strategy permits it.
- Add a renderer/config invariant test for providers using explicit fallback credentials: fallback priority must be numerically lower than primary priority unless intentionally overridden.

## Optional

### 7. Renderer does not validate APISIX enum-like settings before Admin API deploy

Evidence:
- `normalize_router_settings()` passes any string as `balancer.algorithm` and any string list as `fallback_strategy`: `scripts/render-routes.py:185-199`.

APISIX doc reference:
- ai-proxy-multi docs list valid algorithms as `roundrobin` and `chash`, and document fallback strategy values such as `rate_limiting`, `http_429`, and `http_5xx`: <https://apisix.apache.org/docs/apisix/plugins/ai-proxy-multi/>.

Why this impacts determinism/testability:
- A typo fails late at Admin API/runtime rather than in renderer tests, making config failures look like APISIX instability.

Smallest conforming fix / validation:
- Validate `algorithm` and `fallback_strategy` values in `render-routes.py` and add negative tests for invalid values.
- If `chash` is introduced, validate required `hash_on`/`key` fields. Unapproved scope decision: changing the default algorithm from `roundrobin` to `chash` changes load-balancing semantics.

### 8. Health checks are absent; this is not non-standard, but tests should not assume instant dead-upstream avoidance

Evidence:
- Generated `ai-proxy-multi` config includes instances, balancer, fallback strategy, timeout, keepalive, and TLS verification, but no `checks` object: `scripts/render-routes.py:329-340`.

APISIX doc reference:
- ai-proxy-multi docs define optional `checks.active` health check config and note that some common providers do not have official health check endpoints: <https://apisix.apache.org/docs/apisix/plugins/ai-proxy-multi/>.

Why this impacts determinism/testability:
- Without active checks, a dead endpoint may only be discovered during a request and bounded by the configured timeout. Tests should use deterministic 429/500 mock responses for fallback rather than expecting APISIX to pre-skip an unhealthy provider.

Smallest conforming fix / validation:
- Document that production provider health checks are intentionally absent unless a provider exposes a safe health endpoint.
- Unapproved product/scope decision: adding active health checks for real providers could create provider-specific probing behavior and should be decided per provider.

## Defer

### 9. Capability refresh mutates a tracked file and uses live OpenRouter data, but it is secondary to routing determinism

Evidence:
- `configure-routes.sh` writes `conf/model-capabilities.json` in place using live OpenRouter and live `/v1/models`: `scripts/configure-routes.sh:31-37`.

Why defer:
- This can create noisy diffs and metadata variability, but it does not directly determine APISIX route selection, fallback, or load balancing. Revisit after route-ID, catalog-degradation, and APISIX contract tests are fixed.
