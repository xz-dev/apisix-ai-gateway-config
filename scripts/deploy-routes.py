#!/usr/bin/env python3
"""Render and deploy this repo's managed APISIX routes.

This is the single deployment pipeline for generated AI gateway routes:
render desired route JSON, apply desired routes through the APISIX Admin API,
then delete stale routes that were previously managed by this repository.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

MANAGED_BY = "apisix-ai-gateway-config"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", required=True, help="Path to conf/model-pools.json")
    parser.add_argument("--capabilities", help="Path to conf/model-capabilities.json")
    parser.add_argument("--admin-key-file", required=True, help="Path to APISIX Admin API key file")
    parser.add_argument("--admin-url", default="http://127.0.0.1:9180", help="APISIX Admin API base URL")
    parser.add_argument("--catalog-timeout", type=float, default=20.0)
    parser.add_argument(
        "--rendered-dir",
        help="Use an existing rendered route directory instead of rendering live provider catalogs again",
    )
    parser.add_argument(
        "--manifest",
        help="Manifest JSON for --rendered-dir. Required when --rendered-dir is set.",
    )
    return parser.parse_args()


def read_admin_key(path: Path) -> str:
    key = path.read_text(encoding="utf-8").strip()
    if not key:
        raise SystemExit(f"empty APISIX admin key file: {path}")
    return key


def request_json(url: str, *, admin_key: str, method: str = "GET", body: bytes | None = None) -> Any:
    headers = {"X-API-KEY": admin_key, "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = resp.read().decode()
        return json.loads(payload) if payload else {}


def request_status(url: str, *, admin_key: str, method: str) -> int:
    req = urllib.request.Request(url, headers={"X-API-KEY": admin_key}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        exc.read()
        return int(exc.code)


def render_routes(args: argparse.Namespace, out_dir: Path, manifest: Path) -> dict[str, Any]:
    root = Path(__file__).resolve().parents[1]
    cmd = [
        sys.executable,
        str(root / "scripts" / "render-routes.py"),
        "--registry",
        args.registry,
        "--out-dir",
        str(out_dir),
        "--manifest",
        str(manifest),
        "--catalog-timeout",
        str(args.catalog_timeout),
    ]
    if args.capabilities:
        cmd.extend(["--capabilities", args.capabilities])
    subprocess.run(cmd, check=True, cwd=root, env=os.environ.copy())
    return json.loads(manifest.read_text(encoding="utf-8"))


def put_route(admin_url: str, admin_key: str, route_id: str, route_path: Path) -> None:
    body = route_path.read_bytes()
    request_json(f"{admin_url}/apisix/admin/routes/{route_id}", admin_key=admin_key, method="PUT", body=body)
    print(f"configured route {route_id}")


def delete_route(admin_url: str, admin_key: str, route_id: str) -> None:
    status = request_status(f"{admin_url}/apisix/admin/routes/{route_id}", admin_key=admin_key, method="DELETE")
    if status in {200, 202, 204}:
        print(f"deleted route {route_id}")
        return
    if status == 404:
        print(f"route {route_id} already absent")
        return
    raise SystemExit(f"failed to delete route {route_id}: HTTP {status}")


def managed_route_ids(payload: dict[str, Any]) -> set[str]:
    route_ids: set[str] = set()
    for item in payload.get("list") or []:
        route = item.get("value") or item
        if not isinstance(route, dict):
            continue
        labels = route.get("labels") or {}
        route_id = route.get("id")
        if labels.get("managed-by") == MANAGED_BY and isinstance(route_id, str):
            route_ids.add(route_id)
    return route_ids


def validate_rendered_manifest(manifest: dict[str, Any], route_dir: Path) -> list[str]:
    if manifest.get("managed_by") != MANAGED_BY:
        raise SystemExit(f"render manifest must have managed_by={MANAGED_BY!r}")
    model_count = manifest.get("model_count")
    if type(model_count) is not int or model_count < 0:
        raise SystemExit("render manifest model_count must be a non-negative integer")
    route_ids = manifest.get("route_ids")
    if not isinstance(route_ids, list) or not route_ids or not all(isinstance(route_id, str) and route_id for route_id in route_ids):
        raise SystemExit("render manifest route_ids must be a non-empty list of strings")
    desired_ids = [str(route_id) for route_id in route_ids]
    if len(set(desired_ids)) != len(desired_ids):
        raise SystemExit("render manifest route_ids must not contain duplicates")
    for route_id in desired_ids:
        route_path = route_dir / f"route-{route_id}.json"
        if not route_path.is_file():
            raise SystemExit(f"rendered route file missing: {route_path}")
        route = json.loads(route_path.read_text(encoding="utf-8"))
        if not isinstance(route, dict) or route.get("id") != route_id:
            raise SystemExit(f"rendered route file id mismatch: {route_path}")
    return desired_ids


def load_rendered_routes(args: argparse.Namespace, tmpdir: Path) -> tuple[dict[str, Any], Path]:
    rendered_dir = getattr(args, "rendered_dir", None)
    manifest_arg = getattr(args, "manifest", None)
    if rendered_dir:
        if not manifest_arg:
            raise SystemExit("--manifest is required when --rendered-dir is set")
        route_dir = Path(rendered_dir)
        manifest_path = Path(manifest_arg)
        if not route_dir.is_dir():
            raise SystemExit(f"rendered route directory does not exist: {route_dir}")
        if not manifest_path.is_file():
            raise SystemExit(f"render manifest does not exist: {manifest_path}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise SystemExit(f"render manifest must contain a JSON object: {manifest_path}")
        validate_rendered_manifest(manifest, route_dir)
        return manifest, route_dir

    manifest_path = tmpdir / "manifest.json"
    manifest = render_routes(args, tmpdir, manifest_path)
    return manifest, tmpdir


def deploy(args: argparse.Namespace) -> None:
    admin_key = read_admin_key(Path(args.admin_key_file))
    admin_url = args.admin_url.rstrip("/")

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        manifest, route_dir = load_rendered_routes(args, tmpdir)
        desired_ids = validate_rendered_manifest(manifest, route_dir)

        for route_id in desired_ids:
            put_route(admin_url, admin_key, route_id, route_dir / f"route-{route_id}.json")

        current = request_json(f"{admin_url}/apisix/admin/routes", admin_key=admin_key)
        stale_ids = sorted(managed_route_ids(current).difference(desired_ids))
        for route_id in stale_ids:
            delete_route(admin_url, admin_key, route_id)

        print(
            "APISIX AI gateway routes configured: "
            f"{manifest['model_count']} public models, "
            f"{len(desired_ids)} managed routes, unified /v1 pool routing."
        )


def main() -> int:
    deploy(parse_args())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
