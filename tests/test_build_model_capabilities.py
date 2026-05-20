from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build-model-capabilities.py"


def test_build_model_capabilities_converts_litellm_and_local_overrides_win(tmp_path: Path):
    litellm = tmp_path / "model_prices_and_context_window.json"
    base = tmp_path / "model-capabilities.base.json"
    output = tmp_path / "model-capabilities.json"

    litellm.write_text(
        json.dumps(
            {
                "xai/grok-4.3": {
                    "mode": "chat",
                    "max_input_tokens": 1000000,
                    "max_output_tokens": 1000000,
                    "supports_reasoning": True,
                    "supports_function_calling": True,
                    "supports_vision": True,
                },
                "gpt-5.2": {
                    "mode": "chat",
                    "supports_reasoning": True,
                    "supports_minimal_reasoning_effort": True,
                    "supports_xhigh_reasoning_effort": True,
                },
            }
        ),
        encoding="utf-8",
    )
    base.write_text(
        json.dumps(
            {
                "version": 1,
                "models": {
                    "xai/grok-4.3": {
                        "context_window": 1000000,
                        "reasoning": {
                            "enabled": True,
                            "param": "reasoning_effort",
                            "efforts": ["low", "medium", "high"],
                        },
                    },
                    "deepseek/deepseek-v4-pro": {
                        "context_window": 1000000,
                        "reasoning": {
                            "enabled": True,
                            "param": "reasoning_effort",
                            "efforts": ["low", "medium", "high", "xhigh", "max"],
                        },
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--base", str(base), "--litellm", str(litellm), "--output", str(output)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    data = json.loads(output.read_text(encoding="utf-8"))
    models = data["models"]
    assert models["xai/grok-4.3"]["reasoning"]["efforts"] == ["low", "medium", "high"]
    assert models["deepseek/deepseek-v4-pro"]["reasoning"]["efforts"] == ["low", "medium", "high", "xhigh", "max"]
    assert models["gpt-5.2"]["reasoning"]["efforts"] == ["minimal", "xhigh"]


def test_litellm_supports_reasoning_without_effort_flags_does_not_invent_strength(tmp_path: Path):
    litellm = tmp_path / "model_prices_and_context_window.json"
    base = tmp_path / "model-capabilities.base.json"
    output = tmp_path / "model-capabilities.json"
    litellm.write_text(
        json.dumps({"xai/grok-4.3": {"mode": "chat", "supports_reasoning": True}}),
        encoding="utf-8",
    )
    base.write_text('{"version":1,"models":{}}', encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--base", str(base), "--litellm", str(litellm), "--output", str(output)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["models"]["xai/grok-4.3"]["reasoning"] == {
        "enabled": True,
        "param": "reasoning_effort",
        "efforts": [],
    }
