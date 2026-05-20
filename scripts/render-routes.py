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
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

MANAGED_BY = "apisix-ai-gateway-config"
CHAT_URI = "/v1/chat/completions"
MODELS_URI = "/v1/models"
CAPABILITIES_URI = "/v1/model-capabilities"


def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr)


def slug(value: str, *, max_len: int = 80) -> str:
    base = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").lower()
    if len(base) <= max_len:
        return base or "model"
    digest = hashlib.sha1(value.encode()).hexdigest()[:10]
    return f"{base[: max_len - 11].rstrip('-')}-{digest}"


def request_json(url: str, *, api_key: str | None, timeout: float) -> Any:
    headers = {"Accept": "application/json", "User-Agent": "apisix-route-renderer"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def catalog_models(provider: dict[str, Any], env: dict[str, str], timeout: float) -> list[str]:
    fallback = [str(m) for m in provider.get("fallback_models") or [] if str(m)]
    key = next((env.get(name) for name in provider.get("env_vars") or [] if env.get(name)), None)
    url = provider.get("catalog_url")
    if not isinstance(url, str) or not url:
        return filter_models(provider, fallback)
    try:
        payload = request_json(url, api_key=key, timeout=timeout)
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        eprint(f"warning: failed to fetch catalog for {provider.get('id')}: {exc}; using fallback_models")
        return filter_models(provider, fallback)
    items = payload.get("data") if isinstance(payload, dict) else []
    models: list[str] = []
    seen: set[str] = set()
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            model = item.get("id")
            if not isinstance(model, str) or not model or model in seen:
                continue
            seen.add(model)
            models.append(model)
    return filter_models(provider, models or fallback)


def filter_models(provider: dict[str, Any], models: list[str]) -> list[str]:
    include_patterns = [re.compile(str(p)) for p in provider.get("include_model_patterns") or []]
    exclude_patterns = [re.compile(str(p)) for p in provider.get("exclude_model_patterns") or []]
    filtered: list[str] = []
    for model in models:
        if include_patterns and not any(p.search(model) for p in include_patterns):
            continue
        if exclude_patterns and any(p.search(model) for p in exclude_patterns):
            continue
        filtered.append(model)
    return filtered


def provider_env_vars(provider: dict[str, Any], env: dict[str, str]) -> list[str]:
    names: list[str] = []
    for name in provider.get("env_vars") or []:
        if isinstance(name, str) and env.get(name):
            names.append(name)
    return names


def check_required_env(provider: dict[str, Any], env: dict[str, str]) -> None:
    missing = [name for name in provider.get("required_env_vars") or [] if not env.get(str(name))]
    if missing:
        raise SystemExit(f"missing required env var(s) for provider {provider.get('id')}: {', '.join(missing)}")


def public_model(provider: dict[str, Any], upstream_model: str) -> str:
    return str(provider.get("public_prefix") or "") + upstream_model


def instances_for_model(provider: dict[str, Any], upstream_model: str, env_names: list[str], env: dict[str, str]) -> list[dict[str, Any]]:
    instances: list[dict[str, Any]] = []
    for idx, env_name in enumerate(env_names, start=1):
        token = env.get(env_name)
        if not token:
            continue
        instance = {
            "name": f"{provider['id']}-{idx}",
            "provider": provider.get("driver") or "openai-compatible",
            "weight": int(provider.get("instance_weight", 1)),
            "priority": int(provider.get("instance_priority", 0)),
            "auth": {"header": {"Authorization": "Bearer " + token}},
            "options": {"model": str(provider.get("upstream_prefix") or "") + upstream_model},
        }
        endpoint = provider.get("chat_endpoint")
        if endpoint:
            instance["override"] = {"endpoint": endpoint}
        instances.append(instance)
    return instances


def pool_route(route_id: str, provider: dict[str, Any], pub_model: str, upstream_model: str, instances: list[dict[str, Any]], settings: dict[str, Any]) -> dict[str, Any]:
    multi = {
        "instances": instances,
        "balancer": {"algorithm": settings.get("algorithm") or "roundrobin"},
        "fallback_strategy": settings.get("fallback_strategy") or ["http_429", "http_5xx"],
        "timeout": int(settings.get("timeout", 600000)),
        "ssl_verify": bool(settings.get("ssl_verify", True)),
        "keepalive": bool(settings.get("keepalive", True)),
        "keepalive_timeout": int(settings.get("keepalive_timeout", 60000)),
        "keepalive_pool": int(settings.get("keepalive_pool", 30)),
    }
    return {
        "id": route_id,
        "name": f"APISIX pool -> {pub_model}",
        "uri": CHAT_URI,
        "methods": ["POST"],
        "priority": int(provider.get("route_priority", 100)),
        "labels": {
            "managed-by": MANAGED_BY,
            "route-kind": "model-pool",
            "provider": str(provider.get("id") or ""),
            "public-model": pub_model,
            "upstream-model": upstream_model,
        },
        "vars": [["post_arg.model", "==", pub_model]],
        "plugins": {"ai-proxy-multi": multi},
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
            "mocking": {
                "content_type": "application/json",
                "response_status": 200,
                "with_mock_header": False,
                "response_example": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            }
        },
    }
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", required=True)
    parser.add_argument("--capabilities")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--catalog-timeout", type=float, default=20.0)
    args = parser.parse_args()

    registry_path = Path(args.registry)
    registry = json.loads(registry_path.read_text())
    capabilities_path = Path(args.capabilities) if args.capabilities else registry_path.with_name("model-capabilities.json")
    capabilities = json.loads(capabilities_path.read_text()) if capabilities_path.exists() else {}
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    settings = registry.get("router_settings") if isinstance(registry.get("router_settings"), dict) else {}

    routes: list[dict[str, Any]] = []
    catalog: list[dict[str, str]] = []
    seen_public: set[str] = set()

    for provider in registry.get("providers") or []:
        if not isinstance(provider, dict):
            continue
        check_required_env(provider, env)
        env_names = provider_env_vars(provider, env)
        if not env_names:
            eprint(f"warning: provider {provider.get('id')} has no configured API keys; skipping")
            continue
        upstream_models = catalog_models(provider, env, args.catalog_timeout)
        for upstream in upstream_models:
            pub = public_model(provider, upstream)
            if pub in seen_public:
                continue
            seen_public.add(pub)
            inst = instances_for_model(provider, upstream, env_names, env)
            if not inst:
                continue
            route_id = "pool-" + slug(pub)
            routes.append(pool_route(route_id, provider, pub, upstream, inst, settings))
            catalog.append({"id": pub, "owned_by": str(provider.get("owned_by") or provider.get("id") or "apisix")})

    catalog.sort(key=lambda item: item["id"].lower())
    routes.sort(key=lambda r: str(r.get("id")))
    routes.append(models_route(catalog))
    routes.append(model_capabilities_route(capabilities if isinstance(capabilities, dict) else {}, catalog))

    manifest = {"managed_by": MANAGED_BY, "route_ids": [], "model_count": len(catalog), "models": [m["id"] for m in catalog]}
    for route in routes:
        rid = str(route["id"])
        manifest["route_ids"].append(rid)
        path = out_dir / f"route-{rid}.json"
        path.write_text(json.dumps(route, ensure_ascii=False, separators=(",", ":")))

    Path(args.manifest).write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"route_count": len(routes), "model_count": len(catalog), "manifest": str(args.manifest)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
