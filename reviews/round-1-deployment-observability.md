# Review round 1: configuration distribution, deployment mode, and observability

Inputs requested: `plan.md` and `progress.md` were not present in `/root/apisix-ai-gateway-config` during this fresh-context review (`ENOENT`). I inspected the repo files, current git state/diff, compose/config/scripts/tests directly. Pre-review git state had no tracked diff; only `?? .pi/` was untracked.

Validation run:
- `python3 -m py_compile scripts/*.py tests/*.py` passed.
- `python3 -m pytest -q` could not run because `pytest` is not installed (`No module named pytest`).

## Blocker

- None found for the current local deployment mode. The repo is using APISIX's Admin API / etcd path consistently, not standalone route YAML:
  - `conf/config.yaml:26-46` sets `deployment.role: traditional`, `role_traditional.config_provider: etcd`, Admin API, and etcd host.
  - `docker-compose.yml:1-34` runs etcd plus one APISIX node and mounts only bootstrap `conf/config.yaml`.
  - `scripts/deploy-routes.py:83-129` writes route resources through `/apisix/admin/routes/...` and deletes stale managed routes.
  - Official APISIX deployment docs: traditional mode uses `role: traditional` with `role_traditional.config_provider: etcd`; standalone file-driven YAML uses `role: data_plane` with `role_data_plane.config_provider: yaml` (`deployment-modes.md:38,53-55,138-141`, https://apisix.apache.org/docs/apisix/deployment-modes/).
  - Smallest conforming action: keep documenting Admin API/etcd as the single source of truth; do not introduce `apisix.yaml` route files unless intentionally switching to standalone mode.

## Fix now

- **Prometheus is enabled but not externally scrapeable, and request-level external logging/request IDs are not configured.**
  - Repo refs:
    - `conf/config.yaml:11-24` sends default NGINX access logs to stdout and enables only `ai`, `mocking`, `cors`, `ai-proxy-multi`, `ai-proxy`, `ai-rate-limiting`, and `prometheus`.
    - `docker-compose.yml:28-34` publishes only `4000` and `9180`; no Prometheus export port is exposed.
    - `scripts/render-routes.py:329-356`, `359-437` generates routes with only `ai-proxy-multi`/`cors` or `mocking`/`cors`; no `request-id`, `http-logger`, `tcp-logger`, `kafka-logger`, etc.
  - APISIX docs:
    - Prometheus docs say the plugin exports metrics and, for containerized external access, set `plugin_attr.prometheus.export_addr.ip: 0.0.0.0` and scrape `:9091/apisix/prometheus/metrics` (`prometheus.md:36-38,47-54,258-270`, https://apisix.apache.org/docs/apisix/plugins/prometheus/).
    - APISIX plugin docs say a custom `plugins:` list replaces defaults, so omitted plugins are not installed (`terminology/plugin.md:63-82`, https://apisix.apache.org/docs/apisix/terminology/plugin/).
    - `http-logger` and `tcp-logger` docs support JSON/custom log formats and external sinks (`http-logger.md:8,45,92-96`; `tcp-logger.md:46,125-133`).
    - `request-id` docs describe adding a unique ID to track proxied requests (`request-id.md:7`).
  - Why this hurts operability: fallback/load-balancing behavior is mostly opaque. Prometheus may be collecting inside APISIX, but the host cannot scrape it as configured; logs lack a guaranteed request correlation ID and are not sent to the future external logging system.
  - Smallest conforming fix: add `plugin_attr.prometheus.export_addr.ip: 0.0.0.0` and publish `127.0.0.1:9091:9091` or put Prometheus on the Docker network; add `request-id` plus one selected logger plugin to `conf/config.yaml`; render a global rule or per-route logger config including request ID, route/model, status, upstream status/address, latency, and LLM metadata while redacting `Authorization`.
  - Needs user approval: choose the external logging plugin/sink (`http-logger`, `tcp-logger`, `kafka-logger`, etc.) and decide body/token/PII logging policy.

- **Generated `ai-proxy-multi` instances do not configure health checks, so failed instances stay opaque and reactive-only.**
  - Repo refs:
    - `scripts/render-routes.py:312-324` emits each instance with `name`, `provider`, `weight`, `priority`, `auth`, `options`, and `override` only.
    - `scripts/render-routes.py:331-340` sets balancer/fallback/timeout but no `checks`.
    - `conf/model-pools.json:4-15` has router defaults but no health-check schema.
  - APISIX docs:
    - `ai-proxy-multi` supports health checks and `instances.checks.active` (`ai-proxy-multi.md:41-43,105-114`, https://apisix.apache.org/docs/apisix/plugins/ai-proxy-multi/).
    - Health-check docs explain active checks mark failed nodes unhealthy; passive-only checks cannot proactively recover and usually need active checks (`health-check.md:35-47`, https://apisix.apache.org/docs/apisix/tutorials/health-check/).
    - Prometheus `apisix_upstream_status` exists only when health checks are configured (`prometheus.md:117,238-245`).
  - Why this hurts operability: a bad provider/account can remain in round-robin rotation, with every request paying the failure/fallback cost; Prometheus cannot expose upstream health for these instances.
  - Smallest conforming fix: extend `model-pools.json` with optional per-provider/per-instance health-check config and render it to `instances[].checks.active` where providers have safe health endpoints. Where official health endpoints are unavailable, document that explicitly and rely on bounded timeout/fallback plus logging/metrics.

- **`configure-routes.sh` performs a two-pass live deployment and rewrites a tracked config file during deploy.**
  - Repo refs:
    - `scripts/configure-routes.sh:27-37` deploys once, rebuilds capabilities from OpenRouter plus the live gateway `/v1/models`, then deploys again.
    - `scripts/build-model-capabilities.py:20-21,34-43,58-63,290-333` defaults to live LiteLLM/OpenRouter URLs and writes `--output` directly.
    - `scripts/deploy-routes.py:117-129` applies each route sequentially through Admin API, then deletes stale managed routes.
  - APISIX docs: traditional/Admin API/etcd mode expects configuration through the Admin API (`deployment-modes.md:38,53-55`).
  - Why this can race/drift: if the second phase fails, APISIX can be left with new routes/catalog but old capabilities. The output `conf/model-capabilities.json` can drift with external catalogs at deployment time, making tests and rollbacks non-deterministic. In future multi-node etcd distribution, nodes will converge through etcd, but clients can still see mixed route/capability versions during the sequential apply.
  - Smallest conforming fix: split "refresh generated capability artifact" from "deploy". For deploy, build capabilities into a temp artifact from explicit/pinned inputs, render the full desired route set once, validate it, then apply once. Add a deployment version label to all managed routes and update catalog/capability routes last. If the capability file is meant to be committed, make refresh an explicit pre-deploy command instead of mutating it inside `configure-routes.sh`.
  - Needs user approval: decide whether `conf/model-capabilities.json` is a committed source artifact or a generated runtime artifact.

## Optional

- **The local start path can be nondeterministic because the APISIX image is unpinned and readiness is not checked.**
  - Repo refs: `docker-compose.yml:22-34` uses `apache/apisix:latest` and only `depends_on`; `README.md:96-101` runs `docker compose up -d` then immediately `./scripts/configure-routes.sh`.
  - APISIX docs: config/plugin behavior is versioned and static config changes require reload/restart (`terminology/plugin.md:80-82`; `prometheus.md:89`).
  - Why this can hurt stability: `latest` can change plugin schemas/defaults under the repo, and Admin API may not be ready when the deploy script starts.
  - Smallest conforming fix: pin an APISIX image version matching the docs/tests and add compose health checks or an Admin API wait/retry loop before deploying routes.

- **Repo docs misstate `ai-proxy-multi` priority direction for future fallback-provider pools.**
  - Repo refs: `docs/model-pools.md:71,154` says lower priority is preferred / `xai` priority `10` is lower than `0`; `README.md:26-29` describes xAI as lower-priority fallback-style usage; `conf/model-pools.json:119-128` sets xAI priority `10`.
  - APISIX docs: the priority/rate-limit example uses priority `1` as the high-priority instance and priority `0` as the low-priority fallback (`ai-proxy-multi.md:400-446`).
  - Why this matters: current xAI routes are separate public models, so this is not breaking today. But if the repo later combines providers into cross-provider pools, priority `10` would be higher priority than `0`, not a fallback tier.
  - Smallest conforming fix: correct the docs and any future cross-provider examples to say higher priority wins; use a lower numeric priority for fallback tiers when combining providers.

- **The route-contract tests do not cover the observability contract yet.**
  - Repo refs: `tests/test_gateway_route_contract.py:106-209` covers balancer/fallback/timeout/catalog/CORS; `tests/test_render_routes.py:147-177` covers route and CORS generation; neither asserts request IDs, logger plugins, Prometheus exposure, or health checks.
  - APISIX docs: `ai-proxy-multi` exposes LLM request info to access logs/logging plugins (`ai-proxy-multi.md:41-43`), and Prometheus provides LLM/request labels (`prometheus.md:140-218`).
  - Why this matters: observability can regress silently while fallback tests still pass.
  - Smallest conforming fix: once logging and health-check design is approved, add renderer tests for logger/request-id/health-check fields and a verify check for the Prometheus scrape endpoint.

## Defer

- **Future etcd distribution needs an explicit secret-management decision; current Admin API payloads materialize provider keys into route config.**
  - Repo refs:
    - `scripts/render-routes.py:157-173` resolves env vars to values and discards env var names.
    - `scripts/render-routes.py:315-324` embeds `Authorization: Bearer <value>` in each generated route instance.
    - `scripts/deploy-routes.py:83-85` PUTs those route JSON bodies into APISIX/etcd.
    - `README.md:54-94` correctly keeps secrets out of git, but does not mention that the deployed APISIX route config contains the resolved secrets.
  - APISIX docs: APISIX Secret supports environment variables and external secret managers so secrets do not exist in plain text across the platform; `$secret://...`, `$env://...`, and `$ENV://...` can be used in plugin config fields (`terminology/secret.md:33-45,49-57`, https://apisix.apache.org/docs/apisix/terminology/secret/).
  - Why this matters later: a distributed etcd/control-plane setup will replicate these bearer tokens into the config store, backups, and every data plane. Rotation and audit become harder.
  - Smallest conforming fix: preserve credential references in the registry and render `Authorization: Bearer $env://...` or `$secret://...` instead of literal values; configure the chosen secret backend on APISIX nodes.
  - Needs user approval: choose local `$env://` versus Vault/AWS/GCP Secret Manager for the future deployment target.

- **Future multi-node topology should be decided before adding distributed config.**
  - Repo refs: current `docker-compose.yml:1-36` is one traditional APISIX node plus etcd; `conf/config.yaml:26-46` enables Admin API on that node.
  - APISIX docs: deployment modes distinguish traditional, decoupled control/data plane, and standalone; standalone `data_plane` file YAML is a different configuration path (`deployment-modes.md:35-40,53-87,112-141`).
  - Why this matters: adding more APISIX nodes can stay traditional with a shared etcd/Admin API source of truth, or move to decoupled control-plane/data-plane. Mixing that with standalone YAML copies would reintroduce config drift.
  - Smallest conforming fix: document the intended future topology. If decoupled, keep Admin API on the control plane and data planes reading etcd; if traditional, target one Admin API endpoint but let all nodes share the same etcd prefix.
  - Needs user approval: traditional multi-node vs decoupled control/data-plane topology.
