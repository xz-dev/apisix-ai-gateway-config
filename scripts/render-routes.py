#!/usr/bin/env python3
"""Render APISIX Admin API route JSON from conf/model-pools.json.

The renderer expands provider catalogs into explicit APISIX ``ai-proxy-multi``
routes. APISIX AI instances use static upstream ``options.model`` values, so
both direct provider-origin routes and root model resolution rules are expanded
at render time into exact ``post_arg.model`` routes.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from re import Match, Pattern
from typing import Any

MANAGED_BY = "apisix-ai-gateway-config"
CHAT_URI = "/v1/chat/completions"
MODELS_URI = "/v1/models"
CAPABILITIES_URI = "/v1/model-capabilities"
CORS_PREFLIGHT_URI = "/v1/*"
ORIGIN_PREFIX = "origin/"
ROUTE_ID_MAX_LEN = 64
ROUTE_ID_HASH_LEN = 10
ROOT_TARGET_PRIORITY_BASE = 1000
ALLOWED_BALANCER_ALGORITHMS = {"roundrobin", "chash"}
ALLOWED_FALLBACK_STRATEGIES = {"http_429", "http_5xx"}


def cors_plugin() -> dict[str, Any]:
    return {
        "allow_origins": "*",
        "allow_methods": "GET,POST,OPTIONS",
        "allow_headers": "Content-Type,Authorization",
        "expose_headers": "Content-Type",
        "max_age": 3600,
    }


@dataclass(frozen=True)
class RouterSettings:
    algorithm: str
    fallback_strategy: list[str]
    timeout: int
    ssl_verify: bool
    keepalive: bool
    keepalive_timeout: int
    keepalive_pool: int


@dataclass(frozen=True)
class InstanceCredential:
    name: str
    value: str
    priority: int


@dataclass(frozen=True)
class ProviderConfig:
    id: str
    owned_by: str
    upstream_prefix: str
    catalog_url: str | None
    chat_endpoint: str
    driver: str
    credentials: list[InstanceCredential]
    catalog_fallback_models: list[str]
    include_patterns: list[Pattern[str]]
    exclude_patterns: list[Pattern[str]]
    route_priority: int
    instance_weight: int
    allow_catalog_fallback: bool


@dataclass(frozen=True)
class ExpandedModel:
    provider: ProviderConfig
    upstream_model: str
    public_model: str
    route_id: str


@dataclass(frozen=True)
class RootModelRule:
    id: str
    match_regex: Pattern[str]
    model_template: str
    target_templates: list[str]
    fallback_strategy: list[str]
    route_priority: int
    owned_by: str


@dataclass(frozen=True)
class RootRoute:
    rule: RootModelRule
    root_model: str
    targets: list[ExpandedModel]
    route_id: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", required=True)
    parser.add_argument("--capabilities")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--catalog-timeout", type=float, default=20.0)
    parser.add_argument(
        "--catalog-snapshot",
        help="Optional JSON object {'providers': {provider_id: [upstream_model, ...]}}; avoids live catalog refetches.",
    )
    return parser.parse_args()


def safe_slug(value: str) -> str:
    base = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-._").lower()
    return base or "model"


def bounded_hashed_id(prefix: str, value: str) -> str:
    """Return an APISIX-safe text ID with a stable hash suffix.

    APISIX Admin API text IDs are documented as 1-64 characters. Always adding a
    hash suffix keeps IDs stable while avoiding collisions such as ``a/b`` and
    ``a-b`` after slug normalization.
    """

    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:ROUTE_ID_HASH_LEN]
    reserved = len(prefix) + 1 + len(digest)
    if reserved >= ROUTE_ID_MAX_LEN:
        raise SystemExit(f"route ID prefix too long: {prefix!r}")
    base = safe_slug(value)
    max_base_len = ROUTE_ID_MAX_LEN - reserved
    truncated = base[:max_base_len].rstrip("-._") or "model"
    route_id = f"{prefix}{truncated}-{digest}"
    if len(route_id) > ROUTE_ID_MAX_LEN:
        raise AssertionError(f"route ID exceeds {ROUTE_ID_MAX_LEN} chars: {route_id}")
    return route_id


def route_id_for_model(public_model: str) -> str:
    return bounded_hashed_id("pool-", public_model)


def string_list(raw: Any, field: str, owner: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise SystemExit(f"provider {owner} field {field} must be a list")
    values = [value for value in raw if isinstance(value, str) and value]
    if len(values) != len(raw):
        raise SystemExit(f"provider {owner} field {field} must contain only non-empty strings")
    return values


def required_string(raw: dict[str, Any], field: str, provider_label: str) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value:
        raise SystemExit(f"provider {provider_label} must have a non-empty {field}")
    return value


def optional_string(raw: dict[str, Any], field: str, provider_id: str) -> str | None:
    value = raw.get(field)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise SystemExit(f"provider {provider_id} field {field} must be a string")
    return value


def compile_patterns(raw: Any, field: str, provider_id: str) -> list[Pattern[str]]:
    return [re.compile(pattern) for pattern in string_list(raw, field, provider_id)]


def normalize_fallback_strategy(raw: Any, owner: str, *, default: list[str] | None = None) -> list[str]:
    values = string_list(default if raw is None else raw, "fallback_strategy", owner)
    unsupported = [value for value in values if value == "rate_limiting"]
    if unsupported:
        raise SystemExit(
            f"{owner} fallback_strategy includes rate_limiting, but ai-rate-limiting is not configured by this renderer"
        )
    unknown = sorted(set(values).difference(ALLOWED_FALLBACK_STRATEGIES))
    if unknown:
        raise SystemExit(f"{owner} fallback_strategy has unsupported value(s): {', '.join(unknown)}")
    deduped: list[str] = []
    for value in values:
        if value not in deduped:
            deduped.append(value)
    return deduped


def split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def collect_indexed(prefix: str, env: dict[str, str]) -> list[tuple[str, str]]:
    pattern = re.compile(rf"^{re.escape(prefix)}_(\d+)$")
    found: list[tuple[int, str, str]] = []
    for name, value in env.items():
        match = pattern.match(name)
        if match and value:
            found.append((int(match.group(1)), name, value))
    return [(name, value) for _, name, value in sorted(found)]


def collect_credentials(
    *,
    env_names: list[str],
    env_prefixes: list[str],
    priority: int,
    env: dict[str, str],
    fallback: bool = False,
) -> list[InstanceCredential]:
    raw: list[tuple[str, str]] = [(name, env[name]) for name in env_names if env.get(name)]
    for prefix in env_prefixes:
        if env.get(prefix):
            raw.append((prefix, env[prefix]))
        for idx, value in enumerate(split_csv(env.get(f"{prefix}S")), start=1):
            raw.append((f"{prefix}S_{idx}", value))
        raw.extend(collect_indexed(prefix, env))

    credentials: list[InstanceCredential] = []
    seen_values: set[str] = set()
    for _, value in raw:
        if value in seen_values:
            continue
        seen_values.add(value)
        idx = len(credentials) + 1
        name = f"fallback-{idx}" if fallback else str(idx)
        credentials.append(InstanceCredential(name=name, value=value, priority=priority))
    return credentials


def require_env_names(provider_id: str, required_names: list[str], credentials: list[InstanceCredential], env: dict[str, str]) -> None:
    missing = [name for name in required_names if not env.get(name)]
    if missing and not credentials:
        raise SystemExit(f"missing required env var(s) for provider {provider_id}: {', '.join(missing)}")
    if not credentials:
        raise SystemExit(f"provider {provider_id} has no configured API keys")


def normalize_router_settings(registry: dict[str, Any]) -> RouterSettings:
    raw_settings = registry.get("router_settings")
    raw: dict[str, Any] = raw_settings if isinstance(raw_settings, dict) else {}
    timeout = int(raw.get("timeout", 30000))
    if not 1 <= timeout <= 60000:
        raise SystemExit("router_settings.timeout must be between 1 and 60000 ms")
    algorithm = str(raw.get("algorithm") or "roundrobin")
    if algorithm not in ALLOWED_BALANCER_ALGORITHMS:
        raise SystemExit(f"router_settings.algorithm must be one of: {', '.join(sorted(ALLOWED_BALANCER_ALGORITHMS))}")
    return RouterSettings(
        algorithm=algorithm,
        fallback_strategy=normalize_fallback_strategy(
            raw.get("fallback_strategy"), "router_settings", default=["http_429", "http_5xx"]
        ),
        timeout=timeout,
        ssl_verify=bool(raw.get("ssl_verify", True)),
        keepalive=bool(raw.get("keepalive", True)),
        keepalive_timeout=int(raw.get("keepalive_timeout", 60000)),
        keepalive_pool=int(raw.get("keepalive_pool", 30)),
    )


def catalog_fallback_models_from(raw: dict[str, Any], provider_id: str) -> list[str]:
    if "catalog_fallback_models" in raw:
        return string_list(raw.get("catalog_fallback_models"), "catalog_fallback_models", provider_id)
    # Backward-compatible input support. v2 docs use catalog_fallback_models to
    # avoid confusing catalog fallback with runtime model failure fallback.
    return string_list(raw.get("fallback_models"), "fallback_models", provider_id)


def normalize_provider(raw: dict[str, Any], index: int, env: dict[str, str]) -> ProviderConfig:
    provider_id = required_string(raw, "id", f"entry #{index}")
    primary_priority = int(raw.get("instance_priority", 0))
    # Deployment fallback tiers are legacy-compatible. v2 defaults every
    # deployment under a logical provider to the same priority unless explicitly
    # overridden.
    fallback_priority = int(raw.get("fallback_instance_priority", primary_priority))
    primary_credentials = collect_credentials(
        env_names=string_list(raw.get("env_vars"), "env_vars", provider_id),
        env_prefixes=string_list(raw.get("env_var_prefixes"), "env_var_prefixes", provider_id),
        priority=primary_priority,
        env=env,
    )
    fallback_credentials = collect_credentials(
        env_names=string_list(raw.get("fallback_env_vars"), "fallback_env_vars", provider_id),
        env_prefixes=string_list(raw.get("fallback_env_var_prefixes"), "fallback_env_var_prefixes", provider_id),
        priority=fallback_priority,
        env=env,
        fallback=True,
    )
    credentials = primary_credentials + fallback_credentials
    required_names = string_list(raw.get("required_env_vars"), "required_env_vars", provider_id)
    require_env_names(provider_id, required_names, credentials, env)
    return ProviderConfig(
        id=provider_id,
        owned_by=str(raw.get("owned_by") or provider_id),
        upstream_prefix=str(raw.get("upstream_prefix") or ""),
        catalog_url=optional_string(raw, "catalog_url", provider_id),
        chat_endpoint=required_string(raw, "chat_endpoint", provider_id),
        driver=str(raw.get("driver") or "openai-compatible"),
        credentials=credentials,
        catalog_fallback_models=catalog_fallback_models_from(raw, provider_id),
        include_patterns=compile_patterns(raw.get("include_model_patterns"), "include_model_patterns", provider_id),
        exclude_patterns=compile_patterns(raw.get("exclude_model_patterns"), "exclude_model_patterns", provider_id),
        route_priority=int(raw.get("route_priority", 100)),
        instance_weight=int(raw.get("instance_weight", 1)),
        allow_catalog_fallback=bool(raw.get("allow_catalog_fallback", False)),
    )


def normalize_providers(registry: dict[str, Any], env: dict[str, str]) -> list[ProviderConfig]:
    providers = registry.get("providers")
    if not isinstance(providers, list):
        raise SystemExit("registry providers must be a list")
    normalized: list[ProviderConfig] = []
    for index, provider in enumerate(providers, start=1):
        if not isinstance(provider, dict):
            raise SystemExit(f"provider entry #{index} must be an object")
        normalized.append(normalize_provider(provider, index, env))
    return normalized


def normalize_root_rules(registry: dict[str, Any]) -> list[RootModelRule]:
    raw_rules = registry.get("root_model_rules") or []
    if not isinstance(raw_rules, list):
        raise SystemExit("registry root_model_rules must be a list")
    rules: list[RootModelRule] = []
    for index, raw_rule in enumerate(raw_rules, start=1):
        if not isinstance(raw_rule, dict):
            raise SystemExit(f"root_model_rules entry #{index} must be an object")
        rule_id = str(raw_rule.get("id") or f"root-rule-{index}")
        raw_regex = raw_rule.get("match_regex")
        if not isinstance(raw_regex, str) or not raw_regex:
            raise SystemExit(f"root_model_rule {rule_id} must have a non-empty match_regex")
        target_templates = string_list(raw_rule.get("target_templates"), "target_templates", rule_id)
        if not target_templates:
            raise SystemExit(f"root_model_rule {rule_id} must have at least one target_template")
        for template in target_templates:
            if not template.startswith(ORIGIN_PREFIX):
                raise SystemExit(f"root_model_rule {rule_id} target_template must start with {ORIGIN_PREFIX}: {template}")
        rules.append(
            RootModelRule(
                id=rule_id,
                match_regex=re.compile(raw_regex),
                model_template=str(raw_rule.get("model_template") or "{model}"),
                target_templates=target_templates,
                fallback_strategy=normalize_fallback_strategy(
                    raw_rule.get("fallback_strategy"), f"root_model_rule {rule_id}", default=[]
                ),
                route_priority=int(raw_rule.get("route_priority", 500)),
                owned_by=str(raw_rule.get("owned_by") or "apisix-root"),
            )
        )
    return rules


def request_json(url: str, *, api_key: str | None, timeout: float) -> Any:
    headers = {"Accept": "application/json", "User-Agent": "apisix-route-renderer"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def extract_catalog_ids(payload: Any) -> list[str]:
    items = payload.get("data") if isinstance(payload, dict) else []
    models: list[str] = []
    seen: set[str] = set()
    for item in items if isinstance(items, list) else []:
        model = item.get("id") if isinstance(item, dict) else None
        if isinstance(model, str) and model and model not in seen:
            seen.add(model)
            models.append(model)
    return models


def filter_models(provider: ProviderConfig, models: list[str]) -> list[str]:
    filtered: list[str] = []
    for model in models:
        include_ok = not provider.include_patterns or any(pattern.search(model) for pattern in provider.include_patterns)
        exclude_ok = not any(pattern.search(model) for pattern in provider.exclude_patterns)
        if include_ok and exclude_ok:
            filtered.append(model)
    return filtered


def load_catalog_snapshot(path: str | None) -> dict[str, list[str]]:
    if not path:
        return {}
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    providers = raw.get("providers") if isinstance(raw, dict) else None
    if not isinstance(providers, dict):
        raise SystemExit("catalog snapshot must contain a providers object")
    snapshot: dict[str, list[str]] = {}
    for provider_id, models in providers.items():
        if not isinstance(provider_id, str) or not isinstance(models, list) or not all(
            isinstance(model, str) and model for model in models
        ):
            raise SystemExit("catalog snapshot providers must map provider IDs to non-empty string model lists")
        snapshot[provider_id] = models
    return snapshot


def catalog_models(provider: ProviderConfig, env: dict[str, str], timeout: float, snapshot: dict[str, list[str]]) -> list[str]:
    del env
    if provider.id in snapshot:
        return filter_models(provider, snapshot[provider.id])
    if provider.catalog_url is None:
        return filter_models(provider, provider.catalog_fallback_models)
    api_key = provider.credentials[0].value
    try:
        models = extract_catalog_ids(request_json(provider.catalog_url, api_key=api_key, timeout=timeout))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        if not provider.allow_catalog_fallback:
            raise SystemExit(f"failed to fetch catalog for {provider.id}; keeping last-good deploy requires aborting render: {exc}") from exc
        models = provider.catalog_fallback_models
    if not models and not provider.allow_catalog_fallback:
        raise SystemExit(f"catalog for {provider.id} produced no models; keeping last-good deploy requires aborting render")
    return filter_models(provider, models or provider.catalog_fallback_models)


def origin_model_id(provider_id: str, raw_model_id: str) -> str:
    return f"{ORIGIN_PREFIX}{provider_id}/{raw_model_id}"


def parse_origin_model_id(model_id: str) -> tuple[str, str] | None:
    if not model_id.startswith(ORIGIN_PREFIX):
        return None
    rest = model_id[len(ORIGIN_PREFIX) :]
    parts = rest.split("/", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    return parts[0], parts[1]


def expand_provider_models(
    providers: list[ProviderConfig], env: dict[str, str], timeout: float, snapshot: dict[str, list[str]] | None = None
) -> list[ExpandedModel]:
    expanded: list[ExpandedModel] = []
    seen_origin: set[str] = set()
    snapshot = snapshot or {}
    for provider in providers:
        upstream_models = catalog_models(provider, env, timeout, snapshot)
        if not upstream_models:
            raise SystemExit(f"provider {provider.id} produced no public models")
        for upstream in upstream_models:
            public_model = origin_model_id(provider.id, upstream)
            if public_model in seen_origin:
                raise SystemExit(f"duplicate origin model id {public_model}")
            seen_origin.add(public_model)
            expanded.append(ExpandedModel(provider, upstream, public_model, route_id_for_model(public_model)))
    return expanded


def template_values(match: Match[str]) -> dict[str, str]:
    values = dict(match.groupdict())
    if "model" not in values:
        values["model"] = match.group(1) if match.groups() else match.group(0)
    values["match"] = match.group(0)
    return values


def render_template(template: str, values: dict[str, str], rule_id: str) -> str:
    try:
        return template.format(**values)
    except KeyError as exc:
        raise SystemExit(f"root_model_rule {rule_id} template references unknown capture: {exc.args[0]}") from exc


def build_root_routes(rules: list[RootModelRule], expanded: list[ExpandedModel]) -> list[RootRoute]:
    origin_by_id = {model.public_model: model for model in expanded}
    root_routes: dict[str, RootRoute] = {}
    for rule in rules:
        produced_by_rule: dict[str, list[str]] = {}
        for candidate in expanded:
            match = rule.match_regex.match(candidate.upstream_model)
            if not match:
                continue
            values = template_values(match)
            root_model = render_template(rule.model_template, values, rule.id)
            if not root_model or root_model.startswith(ORIGIN_PREFIX):
                raise SystemExit(f"root_model_rule {rule.id} produced invalid root model id: {root_model!r}")
            target_ids = [render_template(template, values, rule.id) for template in rule.target_templates]
            targets = [origin_by_id[target_id] for target_id in target_ids if target_id in origin_by_id]
            if not targets:
                continue
            target_signature = [target.public_model for target in targets]
            if root_model in produced_by_rule:
                if produced_by_rule[root_model] != target_signature:
                    raise SystemExit(f"root_model_rule {rule.id} produced conflicting targets for {root_model}")
                continue
            if root_model in root_routes:
                raise SystemExit(f"duplicate root model id {root_model} produced by root_model_rules")
            route = RootRoute(rule, root_model, targets, route_id_for_model(root_model))
            root_routes[root_model] = route
            produced_by_rule[root_model] = target_signature
    return list(root_routes.values())


def instances_for_model(
    model: ExpandedModel,
    *,
    priority_override: int | None = None,
    name_suffix: str = "",
) -> list[dict[str, Any]]:
    provider = model.provider
    return [
        {
            "name": f"{provider.id}-{credential.name}{name_suffix}",
            "provider": provider.driver,
            "weight": provider.instance_weight,
            "priority": priority_override if priority_override is not None else credential.priority,
            "auth": {"header": {"Authorization": "Bearer " + credential.value}},
            "options": {"model": f"{provider.upstream_prefix}{model.upstream_model}"},
            "override": {"endpoint": provider.chat_endpoint},
        }
        for credential in provider.credentials
    ]


def multi_config(
    *,
    instances: list[dict[str, Any]],
    settings: RouterSettings,
    fallback_strategy: list[str],
) -> dict[str, Any]:
    multi = {
        "instances": instances,
        "balancer": {"algorithm": settings.algorithm},
        "timeout": settings.timeout,
        "ssl_verify": settings.ssl_verify,
        "keepalive": settings.keepalive,
        "keepalive_timeout": settings.keepalive_timeout,
        "keepalive_pool": settings.keepalive_pool,
    }
    if fallback_strategy and len(instances) > 1:
        multi["fallback_strategy"] = fallback_strategy
    return multi


def origin_pool_route(model: ExpandedModel, settings: RouterSettings) -> dict[str, Any]:
    provider = model.provider
    instances = instances_for_model(model)
    multi = multi_config(instances=instances, settings=settings, fallback_strategy=settings.fallback_strategy)
    return {
        "id": model.route_id,
        "name": f"APISIX origin pool -> {model.public_model}",
        "uri": CHAT_URI,
        "methods": ["POST"],
        "priority": provider.route_priority,
        "labels": {
            "managed-by": MANAGED_BY,
            "route-kind": "model-pool",
            "model-scope": "origin",
            "provider": provider.id,
            "public-model": model.public_model,
            "upstream-model": model.upstream_model,
        },
        "vars": [["post_arg.model", "==", model.public_model]],
        "plugins": {"ai-proxy-multi": multi, "cors": cors_plugin()},
    }


def root_route_instances(route: RootRoute) -> list[dict[str, Any]]:
    instances: list[dict[str, Any]] = []
    for target_index, target in enumerate(route.targets):
        target_priority = ROOT_TARGET_PRIORITY_BASE - target_index
        suffix = f"-t{target_index + 1}-{safe_slug(target.upstream_model)[:24]}"
        instances.extend(instances_for_model(target, priority_override=target_priority, name_suffix=suffix))
    names = [instance["name"] for instance in instances]
    if len(names) != len(set(names)):
        raise SystemExit(f"root route {route.root_model} produced duplicate instance names")
    return instances


def root_pool_route(route: RootRoute, settings: RouterSettings) -> dict[str, Any]:
    instances = root_route_instances(route)
    multi = multi_config(instances=instances, settings=settings, fallback_strategy=route.rule.fallback_strategy)
    target_ids = [target.public_model for target in route.targets]
    return {
        "id": route.route_id,
        "name": f"APISIX root model -> {route.root_model}",
        "uri": CHAT_URI,
        "methods": ["POST"],
        "priority": route.rule.route_priority,
        "labels": {
            "managed-by": MANAGED_BY,
            "route-kind": "model-pool",
            "model-scope": "root",
            "root-rule": route.rule.id,
            "public-model": route.root_model,
            "origin-targets": ",".join(target_ids),
        },
        "vars": [["post_arg.model", "==", route.root_model]],
        "plugins": {"ai-proxy-multi": multi, "cors": cors_plugin()},
    }


def cors_preflight_route() -> dict[str, Any]:
    return {
        "id": "main-cors-preflight",
        "name": "CORS preflight handler for OpenAI-compatible /v1 API",
        "uri": CORS_PREFLIGHT_URI,
        "methods": ["OPTIONS"],
        "priority": 10_000,
        "labels": {"managed-by": MANAGED_BY, "route-kind": "cors-preflight"},
        "plugins": {
            "cors": cors_plugin(),
            "mocking": {
                "content_type": "text/plain",
                "response_status": 204,
                "with_mock_header": False,
                "response_example": "",
            },
        },
    }


def models_route(models: list[dict[str, str]]) -> dict[str, Any]:
    payload = {
        "object": "list",
        "data": [
            {"id": item["id"], "object": "model", "owned_by": item["owned_by"]}
            for item in models
        ],
    }
    return {
        "id": "main-models",
        "name": "OpenAI-compatible model list generated from APISIX model pools",
        "uri": MODELS_URI,
        "methods": ["GET"],
        "labels": {"managed-by": MANAGED_BY, "route-kind": "model-catalog"},
        "plugins": {
            "cors": cors_plugin(),
            "mocking": {
                "content_type": "application/json",
                "response_status": 200,
                "with_mock_header": False,
                "response_example": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            }
        },
    }


def useful_capability(capability: Any) -> bool:
    return isinstance(capability, dict) and any(key != "source" for key in capability)


def reasoning_strength(capability: dict[str, Any]) -> int:
    reasoning = capability.get("reasoning")
    if not isinstance(reasoning, dict) or reasoning.get("enabled") is not True:
        return 0
    efforts = reasoning.get("efforts")
    if isinstance(efforts, list) and efforts:
        return 2
    return 1


def clone_capability(capability: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(capability)


def capability_suffix_index(raw_models: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = {}
    for model_id, capability in raw_models.items():
        if not useful_capability(capability):
            continue
        suffix = model_id.split("/", 1)[1] if "/" in model_id else model_id
        index.setdefault(suffix.lower(), []).append(capability)
    return index


def capability_candidates_for_origin(
    origin: ExpandedModel,
    raw_models: dict[str, Any],
    suffix_index: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    provider_id = origin.provider.id
    raw_model = origin.upstream_model
    seen: set[int] = set()
    candidates: list[dict[str, Any]] = []

    def add(candidate: dict[str, Any] | None) -> None:
        if not useful_capability(candidate):
            return
        cap_id = id(candidate)
        if cap_id in seen:
            return
        seen.add(cap_id)
        candidates.append(candidate)

    for key in [origin.public_model, f"{provider_id}/{raw_model}", raw_model]:
        add(raw_models.get(key))
    for candidate in suffix_index.get(raw_model.lower(), []):
        add(candidate)
    return candidates


def capability_for_origin(
    origin: ExpandedModel,
    raw_models: dict[str, Any],
    suffix_index: dict[str, list[dict[str, Any]]],
) -> dict[str, Any] | None:
    candidates = capability_candidates_for_origin(origin, raw_models, suffix_index)
    base: dict[str, Any] | None = None
    base_reasoning = -1
    for candidate in candidates:
        reasoning = reasoning_strength(candidate)
        if base is None:
            base = clone_capability(candidate)
            base_reasoning = reasoning
            continue
        if reasoning > base_reasoning and reasoning > 0:
            base["reasoning"] = clone_capability(candidate).get("reasoning")
            base_reasoning = reasoning
    return base


def capability_for_root(
    route: RootRoute,
    raw_models: dict[str, Any],
    suffix_index: dict[str, list[dict[str, Any]]],
) -> dict[str, Any] | None:
    base: dict[str, Any] | None = None
    base_reasoning = -1
    for target in route.targets:
        candidates = capability_candidates_for_origin(target, raw_models, suffix_index)
        if not candidates:
            continue
        if base is None:
            base = clone_capability(candidates[0])
            base_reasoning = reasoning_strength(base)

        for candidate in candidates:
            candidate_reasoning = reasoning_strength(candidate)
            if candidate_reasoning > base_reasoning:
                base["reasoning"] = clone_capability(candidate).get("reasoning")
                base_reasoning = candidate_reasoning
    return base

def model_capabilities_route(
    capabilities: dict[str, Any],
    catalog: list[dict[str, str]],
    origin_by_id: dict[str, ExpandedModel],
    root_by_id: dict[str, RootRoute],
) -> dict[str, Any]:
    raw_models = capabilities.get("models") if isinstance(capabilities.get("models"), dict) else {}
    suffix_index = capability_suffix_index(raw_models)
    models: dict[str, Any] = {}
    for item in catalog:
        model_id = item["id"]
        capability: dict[str, Any] | None = None
        if model_id in origin_by_id:
            capability = capability_for_origin(origin_by_id[model_id], raw_models, suffix_index)
        elif model_id in root_by_id:
            capability = capability_for_root(root_by_id[model_id], raw_models, suffix_index)
        else:
            raw_capability = raw_models.get(model_id)
            capability = clone_capability(raw_capability) if useful_capability(raw_capability) else None
        if capability:
            models[model_id] = capability

    payload = {
        "version": capabilities.get("version") or 1,
        "object": "model_capability.list",
        "data": [
            {"id": model_id, **value}
            for model_id, value in sorted(models.items(), key=lambda item: item[0].lower())
        ],
        "models": models,
    }
    return {
        "id": "main-model-capabilities",
        "name": "Model capability metadata generated from APISIX capability registry",
        "uri": CAPABILITIES_URI,
        "methods": ["GET"],
        "labels": {"managed-by": MANAGED_BY, "route-kind": "model-capabilities"},
        "plugins": {
            "cors": cors_plugin(),
            "mocking": {
                "content_type": "application/json",
                "response_status": 200,
                "with_mock_header": False,
                "response_example": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            }
        },
    }


def build_catalog(expanded: list[ExpandedModel], root_routes: list[RootRoute]) -> list[dict[str, str]]:
    catalog = [{"id": item.public_model, "owned_by": item.provider.owned_by} for item in expanded]
    catalog.extend({"id": item.root_model, "owned_by": item.rule.owned_by} for item in root_routes)
    catalog.sort(key=lambda item: item["id"].lower())
    return catalog


def build_routes(
    expanded: list[ExpandedModel],
    root_routes: list[RootRoute],
    catalog: list[dict[str, str]],
    capabilities: dict[str, Any],
    settings: RouterSettings,
) -> list[dict[str, Any]]:
    origin_by_id = {model.public_model: model for model in expanded}
    root_by_id = {route.root_model: route for route in root_routes}
    routes = [origin_pool_route(item, settings) for item in expanded]
    routes.extend(root_pool_route(item, settings) for item in root_routes)
    routes.sort(key=lambda route: str(route["id"]))
    routes.append(cors_preflight_route())
    routes.append(models_route(catalog))
    routes.append(model_capabilities_route(capabilities, catalog, origin_by_id, root_by_id))
    return routes


def write_routes(
    routes: list[dict[str, Any]],
    catalog: list[dict[str, str]],
    out_dir: Path,
    manifest_path: Path,
    *,
    origin_model_count: int,
    root_model_count: int,
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "managed_by": MANAGED_BY,
        "route_ids": [],
        "model_count": len(catalog),
        "origin_model_count": origin_model_count,
        "root_model_count": root_model_count,
        "models": [m["id"] for m in catalog],
    }
    seen_route_ids: set[str] = set()
    for route in routes:
        route_id = str(route["id"])
        if len(route_id) > ROUTE_ID_MAX_LEN:
            raise SystemExit(f"route id exceeds {ROUTE_ID_MAX_LEN} characters: {route_id}")
        if route_id in seen_route_ids:
            raise SystemExit(f"duplicate generated route id {route_id}")
        seen_route_ids.add(route_id)
        manifest["route_ids"].append(route_id)
        (out_dir / f"route-{route_id}.json").write_text(json.dumps(route, ensure_ascii=False, separators=(",", ":")))
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest


def load_capabilities(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SystemExit(f"missing capabilities file: {path}")
    capabilities = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(capabilities, dict):
        raise SystemExit(f"capabilities file must contain a JSON object: {path}")
    return capabilities


def main() -> int:
    args = parse_args()
    registry_path = Path(args.registry)
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    if not isinstance(registry, dict):
        raise SystemExit("registry must be a JSON object")
    capabilities_path = Path(args.capabilities) if args.capabilities else registry_path.with_name("model-capabilities.json")
    env = dict(os.environ)

    settings = normalize_router_settings(registry)
    providers = normalize_providers(registry, env)
    root_rules = normalize_root_rules(registry)
    expanded = expand_provider_models(providers, env, args.catalog_timeout, load_catalog_snapshot(args.catalog_snapshot))
    root_routes = build_root_routes(root_rules, expanded)
    catalog = build_catalog(expanded, root_routes)
    routes = build_routes(expanded, root_routes, catalog, load_capabilities(capabilities_path), settings)
    manifest = write_routes(
        routes,
        catalog,
        Path(args.out_dir),
        Path(args.manifest),
        origin_model_count=len(expanded),
        root_model_count=len(root_routes),
    )

    print(
        json.dumps(
            {
                "route_count": len(routes),
                "model_count": len(catalog),
                "origin_model_count": len(expanded),
                "root_model_count": len(root_routes),
                "manifest": str(args.manifest),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
