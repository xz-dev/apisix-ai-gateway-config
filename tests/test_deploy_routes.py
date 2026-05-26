from __future__ import annotations

import importlib.util
import sys

import pytest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "deploy-routes.py"

spec = importlib.util.spec_from_file_location("deploy_routes", SCRIPT)
assert spec is not None
assert spec.loader is not None
deploy_routes = importlib.util.module_from_spec(spec)
sys.modules["deploy_routes"] = deploy_routes
spec.loader.exec_module(deploy_routes)


def test_managed_route_ids_extracts_only_repo_managed_routes():
    payload = {
        "list": [
            {"value": {"id": "main-models", "labels": {"managed-by": "apisix-ai-gateway-config"}}},
            {"value": {"id": "external", "labels": {"managed-by": "other"}}},
            {"id": "pool-test", "labels": {"managed-by": "apisix-ai-gateway-config"}},
            {"value": {"labels": {"managed-by": "apisix-ai-gateway-config"}}},
            {"value": "not-a-route"},
        ]
    }

    assert deploy_routes.managed_route_ids(payload) == {"main-models", "pool-test"}


def test_deploy_pipeline_puts_desired_routes_and_deletes_stale(tmp_path, monkeypatch, capsys):
    admin_key = tmp_path / "admin.key"
    admin_key.write_text("secret\n", encoding="utf-8")
    actions = []

    def fake_render_routes(args, out_dir, manifest_path):
        (out_dir / "route-main-models.json").write_text('{"id":"main-models"}', encoding="utf-8")
        (out_dir / "route-pool-test.json").write_text('{"id":"pool-test"}', encoding="utf-8")
        manifest = {"route_ids": ["main-models", "pool-test"], "model_count": 1}
        manifest_path.write_text("{}", encoding="utf-8")
        actions.append(("render", args.registry))
        return manifest

    def fake_put_route(admin_url, key, route_id, route_path):
        actions.append(("put", admin_url, key, route_id, route_path.read_text(encoding="utf-8")))

    def fake_request_json(url, *, admin_key, method="GET", body=None):
        actions.append(("get", url, admin_key, method, body))
        return {
            "list": [
                {"value": {"id": "main-models", "labels": {"managed-by": "apisix-ai-gateway-config"}}},
                {"value": {"id": "stale", "labels": {"managed-by": "apisix-ai-gateway-config"}}},
                {"value": {"id": "external", "labels": {"managed-by": "other"}}},
            ]
        }

    def fake_delete_route(admin_url, key, route_id):
        actions.append(("delete", admin_url, key, route_id))

    monkeypatch.setattr(deploy_routes, "render_routes", fake_render_routes)
    monkeypatch.setattr(deploy_routes, "put_route", fake_put_route)
    monkeypatch.setattr(deploy_routes, "request_json", fake_request_json)
    monkeypatch.setattr(deploy_routes, "delete_route", fake_delete_route)

    args = SimpleNamespace(
        registry="registry.json",
        capabilities=None,
        admin_key_file=str(admin_key),
        admin_url="http://admin.example/",
        catalog_timeout=20.0,
    )

    deploy_routes.deploy(args)

    assert actions == [
        ("render", "registry.json"),
        ("put", "http://admin.example", "secret", "main-models", '{"id":"main-models"}'),
        ("put", "http://admin.example", "secret", "pool-test", '{"id":"pool-test"}'),
        ("get", "http://admin.example/apisix/admin/routes", "secret", "GET", None),
        ("delete", "http://admin.example", "secret", "stale"),
    ]
    assert "1 public models, 2 managed routes" in capsys.readouterr().out


def test_deploy_does_not_touch_apisix_on_render_failure(tmp_path, monkeypatch):
    admin_key = tmp_path / "admin.key"
    admin_key.write_text("secret\n", encoding="utf-8")

    def failing_render_routes(*_args, **_kwargs):
        raise SystemExit("render failed")

    monkeypatch.setattr(deploy_routes, "render_routes", failing_render_routes)

    args = SimpleNamespace(
        registry="registry.json",
        capabilities=None,
        admin_key_file=str(admin_key),
        admin_url="http://admin.example/",
        catalog_timeout=20.0,
    )

    with pytest.raises(SystemExit):
        deploy_routes.deploy(args)
