from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "render-routes.py"


def run_renderer(
    tmp_path: Path,
    registry: dict,
    *,
    env: dict[str, str] | None = None,
    capabilities: dict | None = None,
    write_capabilities: bool = True,
) -> subprocess.CompletedProcess[str]:
    registry_path = tmp_path / "model-pools.json"
    out_dir = tmp_path / "out"
    manifest = tmp_path / "manifest.json"
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    if write_capabilities:
        (tmp_path / "model-capabilities.json").write_text(
            json.dumps(capabilities or {"version": 1, "models": {}}), encoding="utf-8"
        )
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--registry",
            str(registry_path),
            "--out-dir",
            str(out_dir),
            "--manifest",
            str(manifest),
        ],
        cwd=ROOT,
        env=run_env,
        text=True,
        capture_output=True,
        check=False,
    )


def provider(**overrides):
    data = {
        "id": "test",
        "owned_by": "test-owner",
        "chat_endpoint": "https://example.invalid/v1/chat/completions",
        "driver": "openai-compatible",
        "env_vars": ["TEST_API_KEY"],
        "required_env_vars": ["TEST_API_KEY"],
        "catalog_fallback_models": ["model-a"],
    }
    data.update(overrides)
    return data


def registry_with(*providers, root_model_rules=None, router_settings=None):
    return {
        "version": 2,
        "router_settings": router_settings
        or {
            "algorithm": "roundrobin",
            "fallback_strategy": ["http_429", "http_5xx"],
        },
        "root_model_rules": root_model_rules or [],
        "providers": list(providers),
    }


def load_routes(tmp_path: Path) -> dict[str, dict]:
    return {
        path.name.removeprefix("route-").removesuffix(".json"): json.loads(path.read_text(encoding="utf-8"))
        for path in (tmp_path / "out").glob("route-*.json")
    }


def route_by_model(routes: dict[str, dict], model_id: str) -> dict:
    for route in routes.values():
        if (route.get("labels") or {}).get("public-model") == model_id:
            return route
    raise AssertionError(f"missing route for {model_id}")


def catalog_ids(routes: dict[str, dict]) -> set[str]:
    payload = json.loads(routes["main-models"]["plugins"]["mocking"]["response_example"])
    return {item["id"] for item in payload["data"]}


def capability_payload(routes: dict[str, dict]) -> dict:
    return json.loads(routes["main-model-capabilities"]["plugins"]["mocking"]["response_example"])


def test_renderer_fails_when_provider_entry_is_not_an_object(tmp_path):
    result = run_renderer(tmp_path, registry_with("not-a-provider"), env={"TEST_API_KEY": "secret"})

    assert result.returncode != 0
    assert "provider entry #1 must be an object" in result.stderr


def test_renderer_fails_when_provider_has_no_configured_api_key(tmp_path):
    result = run_renderer(
        tmp_path,
        registry_with(provider(required_env_vars=[])),
        env={"TEST_API_KEY": ""},
    )

    assert result.returncode != 0
    assert "provider test has no configured API keys" in result.stderr


def test_renderer_fails_when_filter_removes_all_models(tmp_path):
    result = run_renderer(
        tmp_path,
        registry_with(provider(include_model_patterns=["does-not-match"])),
        env={"TEST_API_KEY": "secret"},
    )

    assert result.returncode != 0
    assert "provider test produced no public models" in result.stderr


def test_renderer_fails_when_capabilities_file_is_missing(tmp_path):
    result = run_renderer(
        tmp_path,
        registry_with(provider()),
        env={"TEST_API_KEY": "secret"},
        write_capabilities=False,
    )

    assert result.returncode != 0
    assert "missing capabilities file" in result.stderr


def test_renderer_fails_catalog_fetch_by_default_to_preserve_last_good(tmp_path):
    result = run_renderer(
        tmp_path,
        registry_with(provider(catalog_url="https://example.invalid/v1/models")),
        env={"TEST_API_KEY": "secret"},
    )

    assert result.returncode != 0
    assert "keeping last-good deploy requires aborting render" in result.stderr


def test_renderer_uses_catalog_fallback_when_explicitly_allowed(tmp_path):
    result = run_renderer(
        tmp_path,
        registry_with(provider(catalog_url="https://example.invalid/v1/models", allow_catalog_fallback=True)),
        env={"TEST_API_KEY": "secret"},
    )

    assert result.returncode == 0, result.stderr
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["models"] == ["origin/test/model-a"]


def test_renderer_generates_origin_routes_and_no_legacy_provider_prefixed_ids(tmp_path):
    result = run_renderer(tmp_path, registry_with(provider()), env={"TEST_API_KEY": "secret"})

    assert result.returncode == 0, result.stderr
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    routes = load_routes(tmp_path)
    assert manifest["model_count"] == 1
    assert manifest["models"] == ["origin/test/model-a"]
    assert "test/model-a" not in manifest["models"]
    route = route_by_model(routes, "origin/test/model-a")
    assert route["vars"] == [["post_arg.model", "==", "origin/test/model-a"]]
    assert route["labels"]["model-scope"] == "origin"
    assert route["plugins"]["ai-proxy-multi"]["instances"][0]["options"]["model"] == "model-a"
    assert "fallback_strategy" not in route["plugins"]["ai-proxy-multi"]
    assert route["plugins"]["cors"]["allow_origins"] == "*"


def test_renderer_preserves_raw_model_slashes_under_origin_namespace(tmp_path):
    result = run_renderer(
        tmp_path,
        registry_with(provider(catalog_fallback_models=["Qwen/Qwen3.6-35B-A3B"])),
        env={"TEST_API_KEY": "secret"},
    )

    assert result.returncode == 0, result.stderr
    routes = load_routes(tmp_path)
    route = route_by_model(routes, "origin/test/Qwen/Qwen3.6-35B-A3B")
    assert route["vars"] == [["post_arg.model", "==", "origin/test/Qwen/Qwen3.6-35B-A3B"]]
    assert route["plugins"]["ai-proxy-multi"]["instances"][0]["options"]["model"] == "Qwen/Qwen3.6-35B-A3B"


def test_deepseek_root_regex_route_prefers_ollama_then_official_and_uses_http_fallback_only(tmp_path):
    root_rule = {
        "id": "deepseek-series",
        "match_regex": "^(?P<model>deepseek-.+)$",
        "model_template": "{model}",
        "target_templates": ["origin/ollama/{model}", "origin/deepseek/{model}"],
        "fallback_strategy": ["http_429", "http_5xx"],
    }
    result = run_renderer(
        tmp_path,
        registry_with(
            provider(
                id="ollama",
                env_vars=["OLLAMA_KEY"],
                required_env_vars=["OLLAMA_KEY"],
                catalog_fallback_models=["deepseek-v4-pro"],
            ),
            provider(
                id="deepseek",
                driver="deepseek",
                env_vars=["DEEPSEEK_KEY"],
                required_env_vars=["DEEPSEEK_KEY"],
                catalog_fallback_models=["deepseek-v4-pro"],
            ),
            root_model_rules=[root_rule],
        ),
        env={"OLLAMA_KEY": "ollama-secret", "DEEPSEEK_KEY": "deepseek-secret"},
    )

    assert result.returncode == 0, result.stderr
    routes = load_routes(tmp_path)
    root = route_by_model(routes, "deepseek-v4-pro")
    pool = root["plugins"]["ai-proxy-multi"]
    assert root["labels"]["model-scope"] == "root"
    assert root["labels"]["origin-targets"] == "origin/ollama/deepseek-v4-pro,origin/deepseek/deepseek-v4-pro"
    assert pool["fallback_strategy"] == ["http_429", "http_5xx"]
    assert "rate_limiting" not in pool["fallback_strategy"]
    assert [instance["provider"] for instance in pool["instances"]] == ["openai-compatible", "deepseek"]
    assert [instance["priority"] for instance in pool["instances"]] == [1000, 999]
    assert [instance["options"]["model"] for instance in pool["instances"]] == ["deepseek-v4-pro", "deepseek-v4-pro"]

    direct = route_by_model(routes, "origin/ollama/deepseek-v4-pro")
    direct_instances = direct["plugins"]["ai-proxy-multi"]["instances"]
    assert {instance["provider"] for instance in direct_instances} == {"openai-compatible"}


def test_models_catalog_exposes_origin_and_root_ids(tmp_path):
    root_rule = {
        "match_regex": "^(?P<model>deepseek-.+)$",
        "target_templates": ["origin/ollama/{model}", "origin/deepseek/{model}"],
    }
    result = run_renderer(
        tmp_path,
        registry_with(
            provider(id="ollama", env_vars=["OLLAMA_KEY"], required_env_vars=["OLLAMA_KEY"], catalog_fallback_models=["deepseek-v4-pro"]),
            provider(id="deepseek", env_vars=["DEEPSEEK_KEY"], required_env_vars=["DEEPSEEK_KEY"], catalog_fallback_models=["deepseek-v4-pro"]),
            root_model_rules=[root_rule],
        ),
        env={"OLLAMA_KEY": "ok", "DEEPSEEK_KEY": "dk"},
    )

    assert result.returncode == 0, result.stderr
    ids = catalog_ids(load_routes(tmp_path))
    assert ids == {"origin/ollama/deepseek-v4-pro", "origin/deepseek/deepseek-v4-pro", "deepseek-v4-pro"}


def test_capabilities_map_origin_and_root_ids_from_model_centric_reasoning_metadata(tmp_path):
    root_rule = {
        "match_regex": "^(?P<model>deepseek-.+)$",
        "target_templates": ["origin/ollama/{model}", "origin/deepseek/{model}"],
    }
    capabilities = {
        "version": 1,
        "models": {
            "ollama/deepseek-v4-pro": {
                "context_window": 1000000,
                "supports_tools": True,
            },
            "deepseek/deepseek-v4-pro": {
                "context_window": 1000000,
                "reasoning": {
                    "enabled": True,
                    "param": "reasoning_effort",
                    "efforts": ["low", "medium", "high", "xhigh", "max"],
                },
            }
        },
    }
    result = run_renderer(
        tmp_path,
        registry_with(
            provider(id="ollama", env_vars=["OLLAMA_KEY"], required_env_vars=["OLLAMA_KEY"], catalog_fallback_models=["deepseek-v4-pro"]),
            provider(id="deepseek", env_vars=["DEEPSEEK_KEY"], required_env_vars=["DEEPSEEK_KEY"], catalog_fallback_models=["deepseek-v4-pro"]),
            root_model_rules=[root_rule],
        ),
        env={"OLLAMA_KEY": "ok", "DEEPSEEK_KEY": "dk"},
        capabilities=capabilities,
    )

    assert result.returncode == 0, result.stderr
    models = capability_payload(load_routes(tmp_path))["models"]
    assert models["origin/deepseek/deepseek-v4-pro"]["reasoning"]["efforts"] == [
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    ]
    # Origin entry has context/tools only; raw DeepSeek model metadata carries
    # reasoning and should be merged into the chosen origin capability.
    assert models["origin/ollama/deepseek-v4-pro"]["reasoning"]["enabled"] is True
    assert models["deepseek-v4-pro"]["reasoning"]["enabled"] is True
    assert "max" in models["deepseek-v4-pro"]["reasoning"]["efforts"]


def test_origin_and_root_reasoning_comes_from_model_centric_alias_if_provider_origin_is_weak(tmp_path):
    root_rule = {
        "match_regex": "^(?P<model>deepseek-.+)$",
        "target_templates": ["origin/ollama/{model}", "origin/deepseek/{model}"],
    }
    capabilities = {
        "version": 1,
        "models": {
            "origin/ollama/deepseek-v4-pro": {
                "context_window": 12000,
                "reasoning": {"enabled": True, "param": "reasoning_effort", "efforts": []},
            },
            "deepseek/deepseek-v4-pro": {
                "context_window": 12000,
                "reasoning": {
                    "enabled": True,
                    "param": "reasoning_effort",
                    "efforts": ["low", "medium", "high", "xhigh", "max"],
                },
            },
        },
    }
    result = run_renderer(
        tmp_path,
        registry_with(
            provider(id="ollama", env_vars=["OLLAMA_KEY"], required_env_vars=["OLLAMA_KEY"], catalog_fallback_models=["deepseek-v4-pro"]),
            provider(
                id="deepseek",
                driver="deepseek",
                env_vars=["DEEPSEEK_KEY"],
                required_env_vars=["DEEPSEEK_KEY"],
                catalog_fallback_models=["deepseek-v4-pro"],
            ),
            root_model_rules=[root_rule],
        ),
        env={"OLLAMA_KEY": "ok", "DEEPSEEK_KEY": "dk"},
        capabilities=capabilities,
    )

    assert result.returncode == 0, result.stderr
    models = capability_payload(load_routes(tmp_path))["models"]
    assert models["origin/ollama/deepseek-v4-pro"]["reasoning"]["efforts"] == ["low", "medium", "high", "xhigh", "max"]
    assert models["deepseek-v4-pro"]["reasoning"]["efforts"] == ["low", "medium", "high", "xhigh", "max"]


def test_siliconflow_origin_reasoning_enriches_from_raw_qwen_alias(tmp_path):
    capabilities = {
        "version": 1,
        "models": {
            "origin/siliconflow-cn/Qwen/Qwen3.6-35B-A3B": {
                "context_window": 65000,
                "supports_tools": True,
            },
            "Qwen/Qwen3.6-35B-A3B": {
                "context_window": 65000,
                "reasoning": {
                    "enabled": True,
                    "param": "reasoning_effort",
                    "efforts": ["minimal", "low", "medium", "high", "xhigh", "max"],
                },
            },
        },
    }
    result = run_renderer(
        tmp_path,
        registry_with(
            provider(
                id="siliconflow-cn",
                env_vars=["SILICONFLOW_KEY"],
                required_env_vars=["SILICONFLOW_KEY"],
                catalog_fallback_models=["Qwen/Qwen3.6-35B-A3B"],
            )
        ),
        env={"SILICONFLOW_KEY": "sf"},
        capabilities=capabilities,
    )

    assert result.returncode == 0, result.stderr
    models = capability_payload(load_routes(tmp_path))["models"]
    assert models["origin/siliconflow-cn/Qwen/Qwen3.6-35B-A3B"]["reasoning"]["enabled"] is True
    assert models["origin/siliconflow-cn/Qwen/Qwen3.6-35B-A3B"]["reasoning"]["param"] == "reasoning_effort"
    assert models["origin/siliconflow-cn/Qwen/Qwen3.6-35B-A3B"]["reasoning"]["efforts"] == [
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    ]


def test_route_ids_are_unique_and_within_apisix_limit_for_colliding_and_long_models(tmp_path):
    long_model = "very/" + "long-model-name-" * 8
    result = run_renderer(
        tmp_path,
        registry_with(provider(catalog_fallback_models=["a/b", "a-b", long_model])),
        env={"TEST_API_KEY": "secret"},
    )

    assert result.returncode == 0, result.stderr
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    route_ids = manifest["route_ids"]
    assert len(route_ids) == len(set(route_ids))
    assert all(len(route_id) <= 64 for route_id in route_ids)
    assert len([route_id for route_id in route_ids if route_id.startswith("pool-")]) == 3


def test_renderer_rejects_rate_limiting_fallback_without_ai_rate_limiting_config(tmp_path):
    result = run_renderer(
        tmp_path,
        registry_with(provider(), router_settings={"algorithm": "roundrobin", "fallback_strategy": ["rate_limiting"]}),
        env={"TEST_API_KEY": "secret"},
    )

    assert result.returncode != 0
    assert "rate_limiting" in result.stderr


def test_renderer_generates_cors_preflight_route(tmp_path):
    result = run_renderer(tmp_path, registry_with(provider()), env={"TEST_API_KEY": "secret"})

    assert result.returncode == 0, result.stderr
    routes = load_routes(tmp_path)
    route = routes["main-cors-preflight"]
    assert route["uri"] == "/v1/*"
    assert route["methods"] == ["OPTIONS"]
    assert route["priority"] > 1000
    assert route["plugins"]["cors"]["allow_origins"] == "*"
    assert "POST" in route["plugins"]["cors"]["allow_methods"]
    assert "OPTIONS" in route["plugins"]["cors"]["allow_methods"]
    assert "Authorization" in route["plugins"]["cors"]["allow_headers"]
    assert route["plugins"]["mocking"]["response_status"] == 204
