"""Contract tests for APISIX gateway route generation.

The regression class is: a request lands on an exhausted/limited upstream
account and then waits instead of falling through to another deployment. These
contracts assert that the declarative renderer builds one ai-proxy-multi pool
per public model, supports arbitrary primary/fallback credentials, and keeps
upstream timeouts bounded.
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
        "public_prefix": "ollama/",
        "chat_endpoint": "https://ollama.com/v1/chat/completions",
        "driver": "openai-compatible",
        "env_var_prefixes": ["OLLAMA_CLOUD_KEY"],
        "fallback_env_var_prefixes": ["OLLAMA_CLOUD_FALLBACK_KEY"],
        "required_env_vars": [],
        "instance_priority": 100,
        "fallback_instance_priority": 0,
        "fallback_models": ["glm-5.1"],
        "route_priority": 200,
    }
    data.update(overrides)
    return data


def _render(tmp_path: Path, registry: dict, env: dict[str, str]) -> tuple[dict[str, dict], dict]:
    registry_path = tmp_path / "model-pools.json"
    out_dir = tmp_path / "out"
    manifest_path = tmp_path / "manifest.json"
    capabilities_path = tmp_path / "model-capabilities.json"
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    capabilities_path.write_text('{"version":1,"models":{}}', encoding="utf-8")

    run_env = os.environ.copy()
    for key in list(run_env):
        if key.startswith("OLLAMA_CLOUD_KEY") or key.startswith("OLLAMA_CLOUD_FALLBACK_KEY"):
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
        "version": 1,
        "router_settings": {
            "algorithm": "roundrobin",
            "fallback_strategy": ["rate_limiting", "http_429", "http_5xx"],
            "timeout": 30000,
        },
        "providers": [_provider()],
    }
    data.update(overrides)
    return data


def _pool(routes: dict[str, dict]) -> dict:
    return routes["pool-ollama-glm-5-1"]["plugins"]["ai-proxy-multi"]


def _instance_names(pool: dict) -> list[str]:
    return [item["name"] for item in pool["instances"]]


def test_pool_accepts_arbitrary_number_of_primary_ollama_accounts(tmp_path: Path):
    routes, _ = _render(
        tmp_path,
        _registry(),
        {"OLLAMA_CLOUD_KEYS": "ollama-key-a,ollama-key-b,ollama-key-c"},
    )

    pool = _pool(routes)
    instances = pool["instances"]

    assert pool["balancer"] == {"algorithm": "roundrobin"}
    assert pool["fallback_strategy"] == ["rate_limiting", "http_429", "http_5xx"]
    assert _instance_names(pool) == ["ollama-1", "ollama-2", "ollama-3"]
    assert {item["weight"] for item in instances} == {1}
    assert {item["priority"] for item in instances} == {100}
    assert {item["options"]["model"] for item in instances} == {"glm-5.1"}
    assert [item["auth"]["header"]["Authorization"] for item in instances] == [
        "Bearer ollama-key-a",
        "Bearer ollama-key-b",
        "Bearer ollama-key-c",
    ]


def test_pool_accepts_arbitrary_number_of_lower_priority_fallback_accounts(tmp_path: Path):
    routes, _ = _render(
        tmp_path,
        _registry(),
        {
            "OLLAMA_CLOUD_KEY_1": "primary-a",
            "OLLAMA_CLOUD_KEY_2": "primary-b",
            "OLLAMA_CLOUD_FALLBACK_KEYS": "fallback-a,fallback-b,fallback-c",
        },
    )

    pool = _pool(routes)

    assert _instance_names(pool) == [
        "ollama-1",
        "ollama-2",
        "ollama-fallback-1",
        "ollama-fallback-2",
        "ollama-fallback-3",
    ]
    assert [item["priority"] for item in pool["instances"]] == [100, 100, 0, 0, 0]
    assert pool["fallback_strategy"] == ["rate_limiting", "http_429", "http_5xx"]


def test_every_chat_route_uses_ai_proxy_multi_pool_even_single_instance_routes(tmp_path: Path):
    routes, _ = _render(tmp_path, _registry(), {"OLLAMA_CLOUD_KEY_1": "primary-a"})

    chat_routes = {name: route for name, route in routes.items() if route.get("uri") == "/v1/chat/completions"}

    assert chat_routes
    for route in chat_routes.values():
        plugins = route.get("plugins") or {}
        assert "ai-proxy-multi" in plugins
        assert "ai-proxy" not in plugins
        assert len(plugins["ai-proxy-multi"].get("instances") or []) >= 1


def test_pool_has_bounded_upstream_timeout_so_fallback_is_observable(tmp_path: Path):
    routes, _ = _render(tmp_path, _registry(), {"OLLAMA_CLOUD_KEY_1": "primary-a"})

    timeout_ms = _pool(routes).get("timeout")

    assert isinstance(timeout_ms, int)
    assert timeout_ms <= 60_000, "fallback cannot rescue exhausted accounts if each bad upstream waits for minutes"


def test_managed_chat_routes_have_exact_model_matcher_and_managed_label(tmp_path: Path):
    routes, _ = _render(tmp_path, _registry(), {"OLLAMA_CLOUD_KEY_1": "primary-a"})

    main = routes["pool-ollama-glm-5-1"]

    assert main.get("labels", {}).get("managed-by") == MANAGED_BY
    assert ["post_arg.model", "==", "ollama/glm-5.1"] in main.get("vars", [])


def test_models_catalog_matches_public_model_ids_used_by_chat_routes(tmp_path: Path):
    routes, manifest = _render(tmp_path, _registry(), {"OLLAMA_CLOUD_KEY_1": "primary-a"})

    main_catalog = json.loads(routes["main-models"]["plugins"]["mocking"]["response_example"])
    public_ids = {item["id"] for item in main_catalog["data"]}

    assert manifest["models"] == ["ollama/glm-5.1"]
    assert public_ids == {"ollama/glm-5.1"}
    assert ["post_arg.model", "==", "ollama/glm-5.1"] in routes["pool-ollama-glm-5-1"].get("vars", [])
