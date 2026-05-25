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
    write_capabilities: bool = True,
) -> subprocess.CompletedProcess[str]:
    registry_path = tmp_path / "model-pools.json"
    out_dir = tmp_path / "out"
    manifest = tmp_path / "manifest.json"
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    if write_capabilities:
        (tmp_path / "model-capabilities.json").write_text('{"version":1,"models":{}}', encoding="utf-8")
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
        "public_prefix": "test/",
        "chat_endpoint": "https://example.invalid/v1/chat/completions",
        "driver": "openai-compatible",
        "env_vars": ["TEST_API_KEY"],
        "required_env_vars": ["TEST_API_KEY"],
        "fallback_models": ["model-a"],
    }
    data.update(overrides)
    return data


def registry_with(*providers):
    return {
        "version": 1,
        "router_settings": {
            "algorithm": "roundrobin",
            "fallback_strategy": ["http_429", "http_5xx"],
        },
        "providers": list(providers),
    }


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


def test_renderer_fails_on_duplicate_public_model_ids(tmp_path):
    first = provider(id="one", public_prefix="shared/", fallback_models=["model-a"])
    second = provider(id="two", public_prefix="shared/", fallback_models=["model-a"])
    result = run_renderer(tmp_path, registry_with(first, second), env={"TEST_API_KEY": "secret"})

    assert result.returncode != 0
    assert "duplicate public model id shared/model-a" in result.stderr


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


def test_renderer_fails_catalog_fetch_without_explicit_fallback(tmp_path):
    result = run_renderer(
        tmp_path,
        registry_with(provider(catalog_url="https://example.invalid/v1/models")),
        env={"TEST_API_KEY": "secret"},
    )

    assert result.returncode != 0
    assert "failed to fetch catalog for test" in result.stderr


def test_renderer_uses_catalog_fallback_when_explicitly_allowed(tmp_path):
    result = run_renderer(
        tmp_path,
        registry_with(provider(catalog_url="https://example.invalid/v1/models", allow_catalog_fallback=True)),
        env={"TEST_API_KEY": "secret"},
    )

    assert result.returncode == 0, result.stderr
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["models"] == ["test/model-a"]


def test_renderer_generates_routes_for_valid_registry(tmp_path):
    result = run_renderer(tmp_path, registry_with(provider()), env={"TEST_API_KEY": "secret"})

    assert result.returncode == 0, result.stderr
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["model_count"] == 1
    assert "pool-test-model-a" in manifest["route_ids"]
    route = json.loads((tmp_path / "out" / "route-pool-test-model-a.json").read_text(encoding="utf-8"))
    assert route["vars"] == [["post_arg.model", "==", "test/model-a"]]
    assert route["plugins"]["ai-proxy-multi"]["instances"][0]["options"]["model"] == "model-a"
    assert route["plugins"]["cors"]["allow_origins"] == "*"
    assert "POST" in route["plugins"]["cors"]["allow_methods"]
    assert route["plugins"]["cors"]["expose_headers"] == "Content-Type"



def test_renderer_generates_cors_preflight_route(tmp_path):
    result = run_renderer(tmp_path, registry_with(provider()), env={"TEST_API_KEY": "secret"})

    assert result.returncode == 0, result.stderr
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert "main-cors-preflight" in manifest["route_ids"]
    route = json.loads((tmp_path / "out" / "route-main-cors-preflight.json").read_text(encoding="utf-8"))
    assert route["uri"] == "/v1/*"
    assert route["methods"] == ["OPTIONS"]
    assert route["priority"] > 1000
    assert route["plugins"]["cors"]["allow_origins"] == "*"
    assert "POST" in route["plugins"]["cors"]["allow_methods"]
    assert "OPTIONS" in route["plugins"]["cors"]["allow_methods"]
    assert "Authorization" in route["plugins"]["cors"]["allow_headers"]
    assert route["plugins"]["mocking"]["response_status"] == 204
