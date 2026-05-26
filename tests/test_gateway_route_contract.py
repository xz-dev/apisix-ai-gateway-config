"""Contract tests for APISIX gateway route generation.

The regression class is: model routing, account load balancing, and approved
root-model fallback become implicit or untestable. These contracts assert that
renderer output uses canonical origin IDs, explicit root routes, bounded APISIX
fallback semantics, and deterministic route IDs.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "render-routes.py"
MANAGED_BY = "apisix-ai-gateway-config"


def _provider(**overrides):
    data = {
        "id": "ollama",
        "owned_by": "ollama-cloud",
        "chat_endpoint": "https://ollama.com/v1/chat/completions",
        "driver": "openai-compatible",
        "env_var_prefixes": ["OLLAMA_CLOUD_KEY"],
        "required_env_vars": [],
        "instance_priority": 0,
        "catalog_fallback_models": ["glm-5.1"],
        "route_priority": 200,
    }
    data.update(overrides)
    return data


def _render(tmp_path: Path, registry: dict, env: dict[str, str], capabilities: dict | None = None) -> tuple[dict[str, dict], dict]:
    registry_path = tmp_path / "model-pools.json"
    out_dir = tmp_path / "out"
    manifest_path = tmp_path / "manifest.json"
    capabilities_path = tmp_path / "model-capabilities.json"
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    capabilities_path.write_text(json.dumps(capabilities or {"version": 1, "models": {}}), encoding="utf-8")

    run_env = os.environ.copy()
    for key in list(run_env):
        if key.startswith("OLLAMA_CLOUD_KEY") or key.startswith("DEEPSEEK_KEY"):
            run_env.pop(key, None)
    run_env.update(env)

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--registry",
            str(registry_path),
            "--capabilities",
            str(capabilities_path),
            "--out-dir",
            str(out_dir),
            "--manifest",
            str(manifest_path),
        ],
        cwd=REPO_ROOT,
        env=run_env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    routes = {
        path.name.removeprefix("route-").removesuffix(".json"): json.loads(path.read_text(encoding="utf-8"))
        for path in out_dir.glob("route-*.json")
    }
    return routes, json.loads(manifest_path.read_text(encoding="utf-8"))


def _registry(**overrides):
    data = {
        "version": 2,
        "router_settings": {
            "algorithm": "roundrobin",
            "fallback_strategy": ["http_429", "http_5xx"],
            "timeout": 30000,
        },
        "root_model_rules": [],
        "providers": [_provider()],
    }
    data.update(overrides)
    return data


def _route_by_model(routes: dict[str, dict], model_id: str) -> dict:
    for route in routes.values():
        if (route.get("labels") or {}).get("public-model") == model_id:
            return route
    raise AssertionError(f"missing route for {model_id}")


def _pool(routes: dict[str, dict], model_id: str = "origin/ollama/glm-5.1") -> dict:
    return _route_by_model(routes, model_id)["plugins"]["ai-proxy-multi"]


def _instance_names(pool: dict) -> list[str]:
    return [item["name"] for item in pool["instances"]]


def test_origin_pool_accepts_arbitrary_number_of_primary_ollama_accounts(tmp_path: Path):
    routes, _ = _render(
        tmp_path,
        _registry(),
        {"OLLAMA_CLOUD_KEYS": "ollama-key-a,ollama-key-b,ollama-key-c"},
    )

    pool = _pool(routes)
    instances = pool["instances"]

    assert pool["balancer"] == {"algorithm": "roundrobin"}
    assert pool["fallback_strategy"] == ["http_429", "http_5xx"]
    assert _instance_names(pool) == ["ollama-1", "ollama-2", "ollama-3"]
    assert {item["weight"] for item in instances} == {1}
    assert {item["priority"] for item in instances} == {0}
    assert {item["options"]["model"] for item in instances} == {"glm-5.1"}
    assert [item["auth"]["header"]["Authorization"] for item in instances] == [
        "Bearer ollama-key-a",
        "Bearer ollama-key-b",
        "Bearer ollama-key-c",
    ]


def test_single_instance_origin_route_omits_fallback_strategy(tmp_path: Path):
    routes, _ = _render(tmp_path, _registry(), {"OLLAMA_CLOUD_KEY_1": "primary-a"})

    pool = _pool(routes)

    assert "fallback_strategy" not in pool


def test_every_chat_route_uses_ai_proxy_multi_pool_even_single_instance_routes(tmp_path: Path):
    routes, _ = _render(tmp_path, _registry(), {"OLLAMA_CLOUD_KEY_1": "primary-a"})

    chat_routes = {name: route for name, route in routes.items() if route.get("uri") == "/v1/chat/completions"}

    assert chat_routes
    for route in chat_routes.values():
        plugins = route.get("plugins") or {}
        assert "ai-proxy-multi" in plugins
        assert "ai-proxy" not in plugins
        assert len(plugins["ai-proxy-multi"].get("instances") or []) >= 1


def test_pool_has_bounded_upstream_timeout_so_failures_are_observable(tmp_path: Path):
    routes, _ = _render(tmp_path, _registry(), {"OLLAMA_CLOUD_KEY_1": "primary-a"})

    timeout_ms = _pool(routes).get("timeout")

    assert isinstance(timeout_ms, int)
    assert timeout_ms <= 60_000, "APISIX timeout fallback is deferred, so bad upstream waits must stay bounded"


def test_managed_origin_chat_routes_have_exact_model_matcher_and_managed_label(tmp_path: Path):
    routes, _ = _render(tmp_path, _registry(), {"OLLAMA_CLOUD_KEY_1": "primary-a"})

    main = _route_by_model(routes, "origin/ollama/glm-5.1")

    assert main.get("labels", {}).get("managed-by") == MANAGED_BY
    assert main.get("labels", {}).get("model-scope") == "origin"
    assert ["post_arg.model", "==", "origin/ollama/glm-5.1"] in main.get("vars", [])


def test_models_catalog_matches_origin_model_ids_used_by_chat_routes(tmp_path: Path):
    routes, manifest = _render(tmp_path, _registry(), {"OLLAMA_CLOUD_KEY_1": "primary-a"})

    main_catalog = json.loads(routes["main-models"]["plugins"]["mocking"]["response_example"])
    public_ids = {item["id"] for item in main_catalog["data"]}

    assert manifest["models"] == ["origin/ollama/glm-5.1"]
    assert public_ids == {"origin/ollama/glm-5.1"}
    assert "ollama/glm-5.1" not in public_ids
    assert ["post_arg.model", "==", "origin/ollama/glm-5.1"] in _route_by_model(routes, "origin/ollama/glm-5.1").get("vars", [])


def test_root_route_expands_fallback_chain_into_priority_tiers(tmp_path: Path):
    deepseek_provider = _provider(
        id="deepseek",
        owned_by="deepseek-official",
        driver="deepseek",
        env_vars=["DEEPSEEK_KEY"],
        env_var_prefixes=[],
        required_env_vars=["DEEPSEEK_KEY"],
        catalog_fallback_models=["deepseek-v4-pro"],
    )
    root_rule = {
        "id": "deepseek-series",
        "match_regex": "^(?P<model>deepseek-.+)$",
        "model_template": "{model}",
        "target_templates": ["origin/ollama/{model}", "origin/deepseek/{model}"],
        "fallback_strategy": ["http_429", "http_5xx"],
    }
    routes, manifest = _render(
        tmp_path,
        _registry(
            providers=[
                _provider(catalog_fallback_models=["deepseek-v4-pro"]),
                deepseek_provider,
            ],
            root_model_rules=[root_rule],
        ),
        {"OLLAMA_CLOUD_KEY_1": "ollama-key", "DEEPSEEK_KEY": "deepseek-key"},
    )

    assert "deepseek-v4-pro" in manifest["models"]
    root = _route_by_model(routes, "deepseek-v4-pro")
    pool = root["plugins"]["ai-proxy-multi"]
    assert root["labels"]["model-scope"] == "root"
    assert root["labels"]["origin-targets"] == "origin/ollama/deepseek-v4-pro,origin/deepseek/deepseek-v4-pro"
    assert pool["fallback_strategy"] == ["http_429", "http_5xx"]
    assert "rate_limiting" not in pool["fallback_strategy"]
    assert [instance["priority"] for instance in pool["instances"]] == [1000, 999]
    assert [instance["provider"] for instance in pool["instances"]] == ["openai-compatible", "deepseek"]

    origin = _pool(routes, "origin/ollama/deepseek-v4-pro")
    assert {instance["provider"] for instance in origin["instances"]} == {"openai-compatible"}


def test_root_capability_uses_first_reasoning_metadata_available_in_target_order(tmp_path: Path):
    deepseek_provider = _provider(
        id="deepseek",
        owned_by="deepseek-official",
        driver="deepseek",
        env_vars=["DEEPSEEK_KEY"],
        env_var_prefixes=[],
        required_env_vars=["DEEPSEEK_KEY"],
        catalog_fallback_models=["deepseek-v4-pro"],
    )
    capabilities = {
        "version": 1,
        "models": {
            "deepseek/deepseek-v4-pro": {
                "source": "local:model-centric:deepseek-v4-pro",
                "context_window": 1000000,
                "reasoning": {
                    "enabled": True,
                    "param": "reasoning_effort",
                    "efforts": ["low", "medium", "high", "xhigh", "max"],
                },
            }
        },
    }
    root_rule = {
        "id": "deepseek-series",
        "match_regex": "^(?P<model>deepseek-.+)$",
        "target_templates": ["origin/ollama/{model}", "origin/deepseek/{model}"],
    }
    routes, _ = _render(
        tmp_path,
        _registry(
            providers=[
                _provider(catalog_fallback_models=["deepseek-v4-pro"]),
                deepseek_provider,
            ],
            root_model_rules=[root_rule],
        ),
        {"OLLAMA_CLOUD_KEY_1": "ollama-key", "DEEPSEEK_KEY": "deepseek-key"},
        capabilities,
    )

    payload = json.loads(routes["main-model-capabilities"]["plugins"]["mocking"]["response_example"])
    models = payload["models"]
    assert models["origin/ollama/deepseek-v4-pro"]["reasoning"]["enabled"] is True
    assert models["deepseek-v4-pro"]["reasoning"]["efforts"] == ["low", "medium", "high", "xhigh", "max"]


def test_cors_preflight_route_is_high_priority_and_not_model_gated(tmp_path: Path):
    routes, manifest = _render(tmp_path, _registry(), {"OLLAMA_CLOUD_KEY_1": "primary-a"})

    route = routes["main-cors-preflight"]

    assert "main-cors-preflight" in manifest["route_ids"]
    assert route["uri"] == "/v1/*"
    assert route["methods"] == ["OPTIONS"]
    assert route["priority"] > 1000
    assert "vars" not in route, "preflight has no JSON body, so it must not be gated on post_arg.model"
    assert route["plugins"]["cors"]["allow_origins"] == "*"
    assert route["plugins"]["cors"]["allow_methods"] == "GET,POST,OPTIONS"
    assert route["plugins"]["cors"]["allow_headers"] == "Content-Type,Authorization"
    assert route["plugins"]["mocking"]["response_status"] == 204
