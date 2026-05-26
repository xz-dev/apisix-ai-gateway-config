from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify-gateway.py"

spec = importlib.util.spec_from_file_location("verify_gateway", SCRIPT)
assert spec is not None
assert spec.loader is not None
verify_gateway = importlib.util.module_from_spec(spec)
sys.modules["verify_gateway"] = verify_gateway
spec.loader.exec_module(verify_gateway)


def context_with_counts(prefix_counts: dict[str, int], *, strict_live_catalog: bool) -> verify_gateway.VerifyContext:
    ids: list[str] = [
        "origin/ollama/glm-5.1",
        "origin/deepseek/deepseek-v4-pro",
        "origin/deepseek/deepseek-v4-flash",
        "origin/siliconflow-cn/Qwen/Qwen3.6-35B-A3B",
        "origin/xai/grok-4.3",
        "deepseek-v4-pro",
        "deepseek-v4-flash",
    ]

    ids.extend([f"origin/ollama/model-{n}" for n in range(prefix_counts.get("origin/ollama/", 0))])
    ids.extend([f"origin/deepseek/model-{n}" for n in range(prefix_counts.get("origin/deepseek/", 0))])
    ids.extend([f"origin/siliconflow-cn/model-{n}" for n in range(prefix_counts.get("origin/siliconflow-cn/", 0))])
    ids.extend([f"origin/xai/model-{n}" for n in range(prefix_counts.get("origin/xai/", 0))])

    return verify_gateway.VerifyContext(
        admin_url="http://admin",
        gateway_url="http://gateway",
        admin_key="secret",
        admin_routes={"list": []},
        public_catalog={"data": [{"id": model_id} for model_id in ids]},
        capabilities={"models": {}},
        strict_live_catalog=strict_live_catalog,
    )


def test_verify_catalog_count_check_is_lenient_by_default(capsys):
    ctx = context_with_counts(
        {
            "origin/ollama/": 1,
            "origin/deepseek/": 1,
            "origin/siliconflow-cn/": 1,
            "origin/xai/": 1,
        },
        strict_live_catalog=False,
    )
    verify_gateway.check_public_catalog(ctx)

    assert "INFO: provider catalog count check skipped" in capsys.readouterr().out


def test_verify_catalog_count_check_can_be_strict():
    ctx = context_with_counts(
        {
            "origin/ollama/": 1,
            "origin/deepseek/": 1,
            "origin/siliconflow-cn/": 1,
            "origin/xai/": 1,
        },
        strict_live_catalog=True,
    )
    with pytest.raises(SystemExit):
        verify_gateway.check_public_catalog(ctx)
