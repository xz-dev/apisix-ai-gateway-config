Note: I did **not** write `/root/apisix-ai-gateway-config/reviews/round-2-tests-deploy-maintainability.md` because the task also said “Do not edit files”; no-edit wins.

## Blocker

- None found.

## Fix now

1. **`scripts/verify-gateway.py` still has flaky live-catalog count assumptions.**  
   Evidence: `scripts/verify-gateway.py:240-249` requires `origin/ollama/ >= 20`, `origin/siliconflow-cn/ >= 20`, etc. This can fail local validation when provider catalogs legitimately change or when static fallback/degraded catalog mode is used. The static fallback lists are much smaller, e.g. `conf/model-pools.json:48-52` and `conf/model-pools.json:94-96`.  
   Smallest safe fix: keep invariant checks for required known models, no legacy aliases, no non-chat leakage, and route/plugin shape; downgrade provider-family counts to informational output or gate them behind an explicit `--strict-live-catalog` flag.  
   User approval needed: no.

2. **`scripts/configure-routes.sh` can mutate APISIX before a later capability catalog refresh failure.**  
   Evidence: `scripts/configure-routes.sh:31-37` runs `deploy_routes`, then refreshes OpenRouter/public-catalog capabilities, then deploys again. If the refresh at lines 32-36 fails, the first deploy may already have PUT/deleted routes, so the overall command fails after mutation. This weakens the approved “failed catalog refresh/render should fail before PUT/delete” safety story for the documented deploy path (`README.md:98-101`).  
   Smallest safe fix: preflight all catalog/capability refresh work before any Admin API mutation, e.g. render to temp, extract/generated `/v1/models` catalog to temp, build capabilities to temp, then call `deploy-routes.py` once with the temp capability file.  
   User approval needed: likely no, unless capability refresh should intentionally be best-effort.

## Optional

1. **Add a committed deploy failure regression test.**  
   Evidence: `tests/test_render_routes.py:142-150` covers renderer catalog failure, and `tests/test_deploy_routes.py:33-84` covers successful deploy ordering, but there is no committed test asserting `deploy-routes.py` makes no Admin API calls when render fails. I manually validated this behavior, but a test would lock in the last-good guarantee.  
   Smallest safe fix: add a `test_deploy_aborts_before_admin_calls_when_render_fails` monkeypatching `render_routes` to raise and asserting no `put/get/delete` calls.  
   User approval needed: no.

2. **Consider enforcing registry `version == 2`.**  
   Evidence: `scripts/render-routes.py:812-818` reads the registry but does not validate `version`; a manual check showed a `version: 1` registry with legacy `public_prefix` renders successfully as `origin/...`.  
   Smallest safe fix: reject unsupported versions and/or deprecated v1-only fields like `public_prefix`.  
   User approval needed: yes if backward-compatible v1 input support is intentional.

3. **Minor docs/schema wording drift.**  
   Evidence: `docs/model-pools.md:91` says `catalog_fallback_models` is required and refers to a “future explicit degraded-catalog mode,” while `allow_catalog_fallback` is already implemented/documented at `docs/model-pools.md:92` and in `scripts/render-routes.py:402-410`.  
   Smallest safe fix: mark it “required when `catalog_url` is absent; recommended otherwise” and remove “future.”  
   User approval needed: no.

## Defer

- None.

## Correct / validation evidence

- `deploy-routes.py` renders before Admin API mutation: render at `scripts/deploy-routes.py:117-121`, PUT/delete only after at `scripts/deploy-routes.py:123-129`.
- Renderer aborts failed live catalog fetches by default: `scripts/render-routes.py:402-409`.
- Route IDs are bounded and duplicate-checked before manifest write: `scripts/render-routes.py:789-799`.
- Docs/ADRs are broadly consistent on origin/root routing: `README.md:20-34`, `docs/model-pools.md:29-52`, `CONTEXT.md:15-29`, `docs/adr/0001-origin-root-model-routing.md:1-3`, `docs/adr/0002-keep-last-good-catalog-refresh.md:1-3`.
- Tests cover origin/root routing, no legacy model IDs, route ID uniqueness/length, no `rate_limiting`, capabilities/reasoning, and renderer catalog failure: `tests/test_render_routes.py:142-331`, `tests/test_gateway_route_contract.py:110-273`, `tests/test_build_model_capabilities.py:462-519`.

## Commands run

- Read attempts for `plan.md` / `progress.md`: both returned `ENOENT`.
- `git status --short` — exit 0.
- `git diff --name-only` — exit 0.
- `python3 -m pytest tests/test_render_routes.py tests/test_gateway_route_contract.py tests/test_build_model_capabilities.py` — exit 1; `pytest` unavailable (`No module named pytest`).
- `python3 -m py_compile scripts/render-routes.py scripts/deploy-routes.py scripts/verify-gateway.py scripts/build-model-capabilities.py` — exit 0.
- `python3 -m py_compile tests/test_render_routes.py tests/test_gateway_route_contract.py tests/test_build_model_capabilities.py tests/test_deploy_routes.py` — exit 0.
- `git diff --check` — exit 0.
- Manual targeted renderer checks for origin/root IDs, route ID bounds, catalog failure, and `rate_limiting` rejection — exit 0.
- Manual deploy render-failure no-Admin-mutation check — exit 0.