# Review round 1 — APISIX AI Gateway plugin/config correctness

Scope notes:
- `plan.md` and `progress.md` were requested but are absent in this checkout (`ENOENT`). I reviewed the repository directly.
- Git state at review start: only untracked `.pi/`; no tracked diff.
- Validation attempted: `python3 -m py_compile` on the Python scripts passed. `pytest` is not installed (`pytest: command not found`; `python3 -m pytest`: `No module named pytest`).

## Correct / validated

- The runtime mode is coherent with APISIX docs: `conf/config.yaml:26-45` uses traditional mode with etcd and Admin API, matching APISIX deployment-mode docs for traditional mode.
- Generated chat routes use `ai-proxy-multi`, not direct `ai-proxy`: `scripts/render-routes.py:331-355`; `scripts/verify-gateway.py:120-123` also checks for direct `ai-proxy` route bypasses.
- The generated `ai-proxy-multi` instance shape matches the documented plugin schema: `name`, `provider`, `weight`, `priority`, `auth.header.Authorization`, `options.model`, and `override.endpoint` are emitted in `scripts/render-routes.py:315-324`.
- Exact JSON-body routing via `vars: [["post_arg.model", "==", ...]]` in `scripts/render-routes.py:354` matches APISIX radixtree docs for filtering routes by POST JSON body (`post_arg.name` / nested `post_arg.*` examples).

## Blocker

None found that proves the current generated `ai-proxy-multi` schema is invalid.

## Fix now

### 1. Runtime uses `apache/apisix:latest` while the reviewed contract is APISIX 3.15

- Repo evidence: `docker-compose.yml:22-24`; `README.md:5`.
- APISIX doc reference: the provided AI Gateway evidence is versioned under APISIX 3.15, especially `ai-proxy-multi` at <https://apisix.apache.org/docs/apisix/3.15/plugins/ai-proxy-multi/>.
- Why this can cause instability: `ai-proxy-multi`, provider drivers, fallback behavior, and route-variable handling are version-sensitive. Pulling `latest` can silently change the schema or retry semantics while tests/docs still assume 3.15.
- Smallest conforming fix: pin the Docker image to the APISIX version used by the docs/tests, e.g. a 3.15 tag or digest, and upgrade intentionally.
- Approval: requires ops/architecture approval only if tracking `latest` is intentional.

### 2. `fallback_strategy` is applied globally even to one-instance pools

- Repo evidence: global fallback is configured in `conf/model-pools.json:4-10`; it is emitted unconditionally in `scripts/render-routes.py:331-335`. Several providers are one-key/one-instance by default (`conf/model-pools.json:51-58`, `74-87`, `113-120`). The docs explicitly say a one-key model is still a one-instance pool (`docs/model-pools.md:98-102`).
- APISIX doc reference: `ai-proxy-multi` docs describe `fallback_strategy` as forwarding/retrying to the next instance for rate-limiting, 429, or 5xx cases.
- Why this can cause instability: a one-instance route has no next instance. Enabling HTTP fallback there exercises retry/fallback code without real redundancy and can obscure the original provider 429/5xx behavior, making failures look like gateway instability rather than upstream quota/outage.
- Smallest conforming fix: render `fallback_strategy` only when a route has at least two instances, or require a real secondary instance for any provider where fallback is expected. Add a test for single-instance routes preserving upstream errors.
- Approval: product/API approval if changing client-visible error codes is considered a compatibility change.

### 3. `rate_limiting` fallback is configured but no `ai-rate-limiting` plugin config is rendered

- Repo evidence: `rate_limiting` is part of the default fallback list (`conf/model-pools.json:6-9`, `scripts/render-routes.py:191-194`), but generated routes only attach `ai-proxy-multi` and `cors` (`scripts/render-routes.py:355`). `ai-rate-limiting` is merely enabled in the plugin list (`conf/config.yaml:17-24`), and tests assert only the fallback string (`tests/test_gateway_route_contract.py:116-150`; `scripts/verify-gateway.py:149-153`).
- APISIX doc reference: `ai-rate-limiting` examples combine `ai-proxy-multi` with `ai-rate-limiting.instances[]`, where each `instances[].name` must match the `ai-proxy-multi` instance name.
- Why this can cause instability/poor testability: `fallback_strategy: ["rate_limiting", ...]` gives the impression APISIX will proactively skip exhausted instances, but without route-level `ai-rate-limiting` limits keyed to instance names, that part of the strategy has no policy to evaluate. Provider HTTP 429 fallback may still work, but APISIX token-quota exhaustion is not configured.
- Smallest conforming fix: either remove `rate_limiting` from defaults/tests and rely on `http_429`/`http_5xx`, or add registry fields that render `ai-rate-limiting.instances` with names matching generated instances such as `ollama-1`.
- Approval: adding limits requires product/architecture approval for token budgets and windows.

### 4. Generated route IDs can exceed APISIX Admin API's documented 64-character ID limit

- Repo evidence: `slug()` defaults to 80 characters (`scripts/render-routes.py:94-99`) and route IDs prepend `pool-` (`scripts/render-routes.py:303-309`), so generated IDs can be up to 85 characters.
- APISIX doc reference: Admin API Route ID syntax documents text IDs as length 1-64: <https://apisix.apache.org/docs/apisix/3.15/admin-api/#route>.
- Why this can cause instability: live provider catalogs may contain long model IDs. The renderer can produce Admin API route IDs APISIX rejects, causing partial deploys or stale old routes.
- Smallest conforming fix: cap the slug portion to 59 characters including the hash suffix (`64 - len("pool-")`) and add a regression test with a long model ID.
- Approval: none expected.

### 5. Local docs invert APISIX priority semantics

- Repo evidence: `docs/model-pools.md:71` says lower priority is preferred, and `docs/model-pools.md:154` says `priority: 10` is lower than `0`. README also describes xAI as lower-priority fallback-style usage (`README.md:29`). Actual Ollama config uses primary `100` and fallback `0` (`conf/model-pools.json:34-35`), which aligns with APISIX's higher-number-first behavior.
- APISIX doc reference: Admin API upstream docs say nodes with lower priority are used only when all higher-priority nodes are tried/unavailable; route priority docs also state higher value means higher priority.
- Why this can cause instability: the checked-in docs would lead future cross-provider or fallback pools to invert primary/fallback tiers. For example, `xai` priority `10` would be preferred over `0`, not behind it, if combined in one pool.
- Smallest conforming fix: update docs/tests wording to “higher numeric priority is preferred; lower numeric values are fallback tiers,” and only change actual priorities where a real pool needs that behavior.
- Approval: doc fix needs none; changing actual cross-provider priorities requires product/architecture approval.

## Optional

### 6. No APISIX health checks are rendered for AI instances

- Repo evidence: generated instances stop at `override.endpoint` with no `checks` field (`scripts/render-routes.py:315-324`), and the registry has no health-check fields (`conf/model-pools.json:4-129`).
- APISIX doc reference: `ai-proxy-multi` supports instance health-check configuration; the docs note some providers lack official health endpoints, while OpenAI-compatible services may expose usable checks.
- Why this can cause instability: APISIX can only discover a bad endpoint by sending a real user request and then relying on 429/5xx fallback. That makes unhealthy endpoints less observable and can make first requests fail slowly.
- Smallest conforming fix: add optional registry fields for `instances[].checks.active` where a provider has a safe health endpoint; otherwise document intentional omission per provider.
- Approval: requires architecture approval for selected health endpoints, intervals, and failure thresholds.

### 7. “Fallback provider” wording does not match the generated route topology

- Repo evidence: README describes “fallback-provider cases” (`README.md:22`) and xAI fallback-style usage (`README.md:29`), but the renderer creates one route per public model from one provider (`scripts/render-routes.py:300-309`) and instances only from that model's provider credentials (`scripts/render-routes.py:312-326`). Exact `post_arg.model` matching (`scripts/render-routes.py:354`) means `ollama/...` cannot fall through to `xai/...`.
- APISIX doc reference: `ai-proxy-multi` fallback/load balancing happens among `instances[]` on the matched route.
- Why this can cause instability/poor testability: if users expect xAI to backstop other provider-prefixed models, the current config cannot do that. xAI priority only matters inside xAI routes.
- Smallest conforming fix: either remove/clarify fallback-provider wording, or add an explicit cross-provider pool model abstraction where one public model maps to multiple provider instances.
- Approval: cross-provider fallback is a product/architecture decision.

## Defer

- `ai-proxy` is enabled in `conf/config.yaml:21-23`, but generated/verified routes do not use it. This is not a current routing bug; consider removing it later only if no manual/admin routes need direct `ai-proxy`.
