#!/usr/bin/env python3
"""Verify the local APISIX AI gateway's generated routes and metadata."""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

MANAGED_BY = "apisix-ai-gateway-config"
REQUIRED_MODELS = {
    "ollama/glm-5.1",
    "deepseek/deepseek-v4-flash",
    "deepseek/deepseek-v4-pro",
    "siliconflow-cn/Qwen/Qwen3.6-35B-A3B",
    "xai/grok-4.3",
}
NON_CHAT_MARKERS = [
    "embedding",
    "reranker",
    "image",
    "bge",
    "kolors",
    "cosyvoice",
    "sensevoice",
    "telespeech",
    "wan2.",
    "ocr",
]


@dataclass(frozen=True)
class VerifyContext:
    admin_url: str
    gateway_url: str
    admin_key: str
    admin_routes: dict[str, Any]
    public_catalog: dict[str, Any]
    capabilities: dict[str, Any]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--admin-key-file", required=True)
    parser.add_argument("--admin-url", default="http://127.0.0.1:9180")
    parser.add_argument("--gateway-url", default="http://127.0.0.1:4000")
    return parser.parse_args()


def request_json(url: str, *, admin_key: str | None = None) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    if admin_key:
        headers["X-API-KEY"] = admin_key
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def request_status(url: str) -> int:
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        exc.read()
        return int(exc.code)


def load_context(args: argparse.Namespace) -> VerifyContext:
    admin_key = Path(args.admin_key_file).read_text(encoding="utf-8").strip()
    if not admin_key:
        raise SystemExit(f"empty APISIX admin key file: {args.admin_key_file}")
    admin_url = args.admin_url.rstrip("/")
    gateway_url = args.gateway_url.rstrip("/")
    return VerifyContext(
        admin_url=admin_url,
        gateway_url=gateway_url,
        admin_key=admin_key,
        admin_routes=request_json(f"{admin_url}/apisix/admin/routes", admin_key=admin_key),
        public_catalog=request_json(f"{gateway_url}/v1/models"),
        capabilities=request_json(f"{gateway_url}/v1/model-capabilities"),
    )


def route_values(ctx: VerifyContext) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for item in ctx.admin_routes.get("list") or []:
        route = item.get("value") or item
        if isinstance(route, dict):
            values.append(route)
    return values


def managed_routes(ctx: VerifyContext) -> list[dict[str, Any]]:
    return [r for r in route_values(ctx) if (r.get("labels") or {}).get("managed-by") == MANAGED_BY]


def pool_routes(ctx: VerifyContext) -> list[dict[str, Any]]:
    return [r for r in managed_routes(ctx) if (r.get("labels") or {}).get("route-kind") == "model-pool"]


def catalog_ids(ctx: VerifyContext) -> list[str]:
    return [
        item.get("id")
        for item in ctx.public_catalog.get("data") or []
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def check_admin_routes(ctx: VerifyContext) -> None:
    routes = route_values(ctx)
    direct = [r.get("id") for r in routes if "ai-proxy" in (r.get("plugins") or {})]
    require(not direct, f"direct ai-proxy route violates unified pool routing: {direct}")

    managed = managed_routes(ctx)
    ids = {r.get("id") for r in managed}
    require("main-models" in ids, "missing managed /v1/models catalog route")
    require("main-model-capabilities" in ids, "missing managed /v1/model-capabilities route")

    pools = pool_routes(ctx)
    require(len(pools) >= 40, f"expected managed provider pools, got only {len(pools)}")
    for route in pools:
        plugins = route.get("plugins") or {}
        multi = plugins.get("ai-proxy-multi") or {}
        require(
            route.get("uri") == "/v1/chat/completions" and "ai-proxy-multi" in plugins,
            f"managed model route is not an ai-proxy-multi chat pool: {route.get('id')}",
        )
        require(
            multi.get("fallback_strategy") == ["http_429", "http_5xx"],
            f"route missing 429/5xx fallback strategy: {route.get('id')}",
        )

    print(
        json.dumps(
            {
                "managed_route_count": len(managed),
                "pool_route_count": len(pools),
                "sample_route_ids": sorted(str(i) for i in ids)[:8],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def check_instance_priorities(ctx: VerifyContext) -> None:
    pools = pool_routes(ctx)
    ollama = next((r for r in pools if (r.get("labels") or {}).get("public-model") == "ollama/glm-5.1"), None)
    require(ollama is not None, "missing ollama/glm-5.1 pool")
    ollama_instances = (((ollama.get("plugins") or {}).get("ai-proxy-multi") or {}).get("instances") or [])
    require(
        len(ollama_instances) >= 2,
        f"ollama/glm-5.1 should have two configured Ollama Cloud instances, got {len(ollama_instances)}",
    )
    require(
        sorted({i.get("priority", 0) for i in ollama_instances}) == [0],
        "Ollama Cloud load-balancing instances should share priority 0",
    )

    xai = next((r for r in pools if (r.get("labels") or {}).get("public-model") == "xai/grok-4.3"), None)
    require(xai is not None, "missing xai/grok-4.3 fallback-provider pool")
    xai_instances = (((xai.get("plugins") or {}).get("ai-proxy-multi") or {}).get("instances") or [{}])
    require(xai_instances[0].get("priority") == 10, "xAI fallback-provider instance should use priority 10")


def check_public_catalog(ctx: VerifyContext) -> None:
    ids = catalog_ids(ctx)
    missing = sorted(REQUIRED_MODELS.difference(ids))
    require(not missing, f"missing public models: {missing}")
    counts = {prefix: sum(1 for model_id in ids if model_id.startswith(prefix)) for prefix in ["ollama/", "deepseek/", "siliconflow-cn/", "xai/"]}
    require(
        counts["ollama/"] >= 20 and counts["deepseek/"] >= 2 and counts["siliconflow-cn/"] >= 20 and counts["xai/"] >= 1,
        f"provider catalog counts too low: {counts}",
    )
    non_chat = [model_id for model_id in ids if any(marker in model_id.lower() for marker in NON_CHAT_MARKERS)]
    require(not non_chat, f"non-chat models leaked into chat catalog: {non_chat[:10]}")
    print(json.dumps({"catalog_count": len(ids), "counts": counts, "sample": ids[:8]}, ensure_ascii=False, indent=2))


def check_model_capabilities(ctx: VerifyContext) -> None:
    models = ctx.capabilities.get("models") or {}
    glm = models.get("ollama/glm-5.1") or {}
    reasoning = glm.get("reasoning") or {}
    require(reasoning.get("enabled") is True, "ollama/glm-5.1 should expose reasoning.enabled=true")
    require(
        {"low", "medium", "high"}.issubset(set(reasoning.get("efforts") or [])),
        "ollama/glm-5.1 should expose low/medium/high reasoning efforts",
    )
    qwen = models.get("siliconflow-cn/Qwen/Qwen3.6-35B-A3B") or {}
    qwen_reasoning = qwen.get("reasoning") or {}
    require(
        qwen_reasoning.get("enabled") is False,
        "siliconflow-cn/Qwen/Qwen3.6-35B-A3B should expose reasoning.enabled=false",
    )
    print(
        json.dumps(
            {
                "capability_count": len(models),
                "reasoning_model": "ollama/glm-5.1",
                "reasoning_enabled": reasoning.get("enabled"),
                "reasoning_efforts": reasoning.get("efforts"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def check_absent_surfaces(ctx: VerifyContext) -> None:
    expectations = {
        "/siliconflow-cn/v1/models": {404, 405},
        "/siliconflow-cn/v1/chat/completions": {404, 405},
        "/v1/model/info": {404},
        "/model/info": {404},
    }
    for path, allowed in expectations.items():
        status = request_status(f"{ctx.gateway_url}{path}")
        print(f"{path}: HTTP {status}")
        require(status in allowed, f"expected {path} to return one of {sorted(allowed)}")


CHECKS: list[tuple[str, Callable[[VerifyContext], None]]] = [
    ("APISIX Admin API managed model routes", check_admin_routes),
    ("APISIX pool instance priorities", check_instance_priorities),
    ("/v1/models public catalog", check_public_catalog),
    ("/v1/model-capabilities reasoning metadata", check_model_capabilities),
    ("unsupported/provider-specific surfaces absent", check_absent_surfaces),
]


def main() -> int:
    ctx = load_context(parse_args())
    for title, check in CHECKS:
        print(f"--- {title} ---")
        check(ctx)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
