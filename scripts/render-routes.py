#!/usr/bin/env python3
"""Render APISIX Admin API route JSON from conf/model-pools.json.

This script expands provider catalogs into explicit APISIX pool routes. APISIX
ai-proxy-multi has static per-instance options.model, so the clean gateway
represents each public model id as one route that selects a pool.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from re import Pattern
from typing import Any

MANAGED_BY = "apisix-ai-gateway-config"
CHAT_URI = "/v1/chat/completions"
MODELS_URI = "/v1/models"
CAPABILITIES_URI = "/v1/model-capabilities"
CORS_PREFLIGHT_URI = "/v1/*"


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
    public_prefix: str
    upstream_prefix: str
    catalog_url: str | None
    chat_endpoint: str
    driver: str
    credentials: list[InstanceCredential]
    fallback_models: list[str]
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", required=True)
    parser.add_argument("--capabilities")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--catalog-timeout", type=float, default=20.0)
    return parser.parse_args()


def slug(value: str, *, max_len: int = 80) -> str:
    base = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").lower()
    if len(base) <= max_len:
        return base or "model"
    digest = hashlib.sha1(value.encode()).hexdigest()[:10]
    return f"{base[: max_len - 11].rstrip('-')}-{digest}"


def string_list(raw: Any, field: str, provider_id: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise SystemExit(f"provider {provider_id} field {field} must be a list")
    values = [value for value in raw if isinstance(value, str) and value]
    if len(values) != len(raw):
        raise SystemExit(f"provider {provider_id} field {field} must contain only non-empty strings")
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
    return RouterSettings(
        algorithm=str(raw.get("algorithm") or "roundrobin"),
        fallback_strategy=string_list(raw.get("fallback_strategy") or ["rate_limiting", "http_429", "http_5xx"], "fallback_strategy", "router_settings"),
        timeout=timeout,
        ssl_verify=bool(raw.get("ssl_verify", True)),
        keepalive=bool(raw.get("keepalive", True)),
        keepalive_timeout=int(raw.get("keepalive_timeout", 60000)),
        keepalive_pool=int(raw.get("keepalive_pool", 30)),
    )


def normalize_provider(raw: dict[str, Any], index: int, env: dict[str, str]) -> ProviderConfig:
    provider_id = required_string(raw, "id", f"entry #{index}")
    primary_priority = int(raw.get("instance_priority", 0))
    fallback_priority = int(raw.get("fallback_instance_priority", primary_priority - 100))
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
        public_prefix=str(raw.get("public_prefix") or ""),
        upstream_prefix=str(raw.get("upstream_prefix") or ""),
        catalog_url=optional_string(raw, "catalog_url", provider_id),
        chat_endpoint=required_string(raw, "chat_endpoint", provider_id),
        driver=str(raw.get("driver") or "openai-compatible"),
        credentials=credentials,
        fallback_models=string_list(raw.get("fallback_models"), "fallback_models", provider_id),
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


def catalog_models(provider: ProviderConfig, env: dict[str, str], timeout: float) -> list[str]:
    if provider.catalog_url is None:
        return filter_models(provider, provider.fallback_models)
    api_key = provider.credentials[0].value
    try:
        models = extract_catalog_ids(request_json(provider.catalog_url, api_key=api_key, timeout=timeout))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        if not provider.allow_catalog_fallback:
            raise SystemExit(f"failed to fetch catalog for {provider.id}: {exc}") from exc
        models = provider.fallback_models
    return filter_models(provider, models or provider.fallback_models)


def expand_provider_models(providers: list[ProviderConfig], env: dict[str, str], timeout: float) -> list[ExpandedModel]:
    expanded: list[ExpandedModel] = []
    seen_public: set[str] = set()
    for provider in providers:
        upstream_models = catalog_models(provider, env, timeout)
        if not upstream_models:
            raise SystemExit(f"provider {provider.id} produced no public models")
        for upstream in upstream_models:
            public_model = f"{provider.public_prefix}{upstream}"
            if public_model in seen_public:
                raise SystemExit(f"duplicate public model id {public_model}")
            seen_public.add(public_model)
            expanded.append(ExpandedModel(provider, upstream, public_model, "pool-" + slug(public_model)))
    return expanded


def instances_for_model(model: ExpandedModel, env: dict[str, str]) -> list[dict[str, Any]]:
    del env
    provider = model.provider
    return [
        {
            "name": f"{provider.id}-{credential.name}",
            "provider": provider.driver,
            "weight": provider.instance_weight,
            "priority": credential.priority,
            "auth": {"header": {"Authorization": "Bearer " + credential.value}},
            "options": {"model": f"{provider.upstream_prefix}{model.upstream_model}"},
            "override": {"endpoint": provider.chat_endpoint},
        }
        for credential in provider.credentials
    ]


def pool_route(model: ExpandedModel, settings: RouterSettings, env: dict[str, str]) -> dict[str, Any]:
    provider = model.provider
    multi = {
        "instances": instances_for_model(model, env),
        "balancer": {"algorithm": settings.algorithm},
        "fallback_strategy": settings.fallback_strategy,
        "timeout": settings.timeout,
        "ssl_verify": settings.ssl_verify,
        "keepalive": settings.keepalive,
        "keepalive_timeout": settings.keepalive_timeout,
        "keepalive_pool": settings.keepalive_pool,
    }
    return {
        "id": model.route_id,
        "name": f"APISIX pool -> {model.public_model}",
        "uri": CHAT_URI,
        "methods": ["POST"],
        "priority": provider.route_priority,
        "labels": {
            "managed-by": MANAGED_BY,
            "route-kind": "model-pool",
            "provider": provider.id,
            "public-model": model.public_model,
            "upstream-model": model.upstream_model,
        },
        "vars": [["post_arg.model", "==", model.public_model]],
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


def model_capabilities_route(capabilities: dict[str, Any], catalog: list[dict[str, str]]) -> dict[str, Any]:
    catalog_ids = {item["id"] for item in catalog}
    raw_models = capabilities.get("models") if isinstance(capabilities.get("models"), dict) else {}
    models = {
        model_id: value
        for model_id, value in raw_models.items()
        if model_id in catalog_ids and isinstance(value, dict)
    }
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


def build_catalog(expanded: list[ExpandedModel]) -> list[dict[str, str]]:
    catalog = [{"id": item.public_model, "owned_by": item.provider.owned_by} for item in expanded]
    catalog.sort(key=lambda item: item["id"].lower())
    return catalog


def build_routes(expanded: list[ExpandedModel], catalog: list[dict[str, str]], capabilities: dict[str, Any], settings: RouterSettings, env: dict[str, str]) -> list[dict[str, Any]]:
    routes = [pool_route(item, settings, env) for item in expanded]
    routes.sort(key=lambda route: str(route["id"]))
    routes.append(cors_preflight_route())
    routes.append(models_route(catalog))
    routes.append(model_capabilities_route(capabilities, catalog))
    return routes


def write_routes(routes: list[dict[str, Any]], catalog: list[dict[str, str]], out_dir: Path, manifest_path: Path) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"managed_by": MANAGED_BY, "route_ids": [], "model_count": len(catalog), "models": [m["id"] for m in catalog]}
    for route in routes:
        route_id = str(route["id"])
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
    expanded = expand_provider_models(providers, env, args.catalog_timeout)
    catalog = build_catalog(expanded)
    routes = build_routes(expanded, catalog, load_capabilities(capabilities_path), settings, env)
    manifest = write_routes(routes, catalog, Path(args.out_dir), Path(args.manifest))

    print(json.dumps({"route_count": len(routes), "model_count": len(catalog), "manifest": str(args.manifest)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
