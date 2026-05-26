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


def test_openrouter_fallback_writes_siliconflow_reasoning_above_litellm_only(tmp_path: Path):
    litellm = tmp_path / "model_prices_and_context_window.json"
    openrouter = tmp_path / "openrouter-models.json"
    catalog = tmp_path / "public-catalog.json"
    base = tmp_path / "model-capabilities.base.json"
    output = tmp_path / "model-capabilities.json"

    litellm.write_text(
        json.dumps(
            {
                "Qwen/Qwen3.6-35B-A3B": {
                    "mode": "chat",
                    "supports_reasoning": True,
                },
                "xai/grok-4.3": {
                    "mode": "chat",
                    "supports_reasoning": True,
                },
            }
        ),
        encoding="utf-8",
    )
    openrouter.write_text(
        json.dumps(
            {
                "data": [
                    {
                        "id": "qwen/qwen3.6-35b-a3b",
                        "context_length": 262144,
                        "top_provider": {"max_completion_tokens": 262144},
                        "architecture": {"input_modalities": ["text"]},
                        "supported_parameters": ["max_tokens", "reasoning", "tools"],
                    },
                    {
                        "id": "xai/grok-4.3",
                        "supported_parameters": ["reasoning"],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    catalog.write_text(
        json.dumps(
            {
                "data": [
                    {"id": "siliconflow-cn/Qwen/Qwen3.6-35B-A3B"},
                    {"id": "xai/grok-4.3"},
                ]
            }
        ),
        encoding="utf-8",
    )
    base.write_text('{"version":1,"models":{}}', encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--base",
            str(base),
            "--litellm",
            str(litellm),
            "--openrouter",
            str(openrouter),
            "--public-catalog",
            str(catalog),
            "--output",
            str(output),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    data = json.loads(output.read_text(encoding="utf-8"))
    qwen = data["models"]["siliconflow-cn/Qwen/Qwen3.6-35B-A3B"]
    assert qwen["source"] == "openrouter:models:qwen/qwen3.6-35b-a3b"
    assert qwen["context_window"] == 262144
    assert qwen["max_output_tokens"] == 262144
    assert qwen["supports_tools"] is True
    assert qwen["reasoning"] == {
        "enabled": True,
        "param": "reasoning_effort",
        "efforts": ["minimal", "low", "medium", "high", "xhigh"],
    }
    assert data["models"]["xai/grok-4.3"]["reasoning"]["efforts"] == []
    assert data["generated_from"]["openrouter_siliconflow_fallback_count"] == 1



def test_openrouter_overlay_overrides_litellm_reasoning_and_copies_metadata(tmp_path: Path):
    litellm = tmp_path / "model_prices_and_context_window.json"
    openrouter = tmp_path / "openrouter-models.json"
    base = tmp_path / "model-capabilities.base.json"
    output = tmp_path / "model-capabilities.json"

    litellm.write_text(
        json.dumps(
            {
                "provider/model-a": {
                    "mode": "chat",
                    "max_input_tokens": 123,
                    "max_output_tokens": 456,
                    "supports_reasoning": True,
                    "supports_minimal_reasoning_effort": True,
                    "supports_function_calling": False,
                    "supports_vision": False,
                }
            }
        ),
        encoding="utf-8",
    )
    openrouter.write_text(
        json.dumps(
            {
                "data": [
                    {
                        "id": "provider/model-a",
                        "context_length": 999,
                        "top_provider": {"max_completion_tokens": 111},
                        "supported_parameters": ["tools", "tool_choice", "reasoning"],
                        "architecture": {"input_modalities": ["text", "image"]},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    base.write_text('{"version":1,"models":{}}', encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--base",
            str(base),
            "--litellm",
            str(litellm),
            "--openrouter",
            str(openrouter),
            "--output",
            str(output),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    data = json.loads(output.read_text(encoding="utf-8"))
    model = data["models"]["provider/model-a"]
    assert model["context_window"] == 999
    assert model["max_output_tokens"] == 111
    assert model["supports_tools"] is True
    assert model["supports_vision"] is True
    assert model["reasoning"] == {
        "enabled": True,
        "param": "reasoning",
        "efforts": ["none", "minimal", "low", "medium", "high", "xhigh"],
    }


def test_local_override_wins_over_openrouter_overlay(tmp_path: Path):
    litellm = tmp_path / "model_prices_and_context_window.json"
    openrouter = tmp_path / "openrouter-models.json"
    base = tmp_path / "model-capabilities.base.json"
    output = tmp_path / "model-capabilities.json"

    litellm.write_text(
        json.dumps({"provider/model-a": {"mode": "chat", "supports_reasoning": True}}),
        encoding="utf-8",
    )
    openrouter.write_text(
        json.dumps(
            {
                "data": [
                    {
                        "id": "provider/model-a",
                        "supported_parameters": ["reasoning_effort"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    base.write_text(
        json.dumps(
            {
                "version": 1,
                "models": {
                    "provider/model-a": {
                        "reasoning": {
                            "enabled": True,
                            "param": "reasoning_effort",
                            "efforts": ["low", "high"],
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--base",
            str(base),
            "--litellm",
            str(litellm),
            "--openrouter",
            str(openrouter),
            "--output",
            str(output),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["models"]["provider/model-a"]["reasoning"] == {
        "enabled": True,
        "param": "reasoning_effort",
        "efforts": ["low", "high"],
    }


def test_generated_base_entries_do_not_become_local_overrides_on_rerun(tmp_path: Path):
    litellm = tmp_path / "model_prices_and_context_window.json"
    openrouter = tmp_path / "openrouter-models.json"
    base = tmp_path / "model-capabilities.json"
    output = tmp_path / "model-capabilities.next.json"

    litellm.write_text(
        json.dumps(
            {
                "provider/model-a": {
                    "mode": "chat",
                    "max_input_tokens": 222,
                    "supports_reasoning": True,
                },
                "provider/litellm-only": {
                    "mode": "chat",
                    "max_input_tokens": 333,
                    "supports_function_calling": True,
                },
            }
        ),
        encoding="utf-8",
    )
    openrouter.write_text(
        json.dumps(
            {
                "data": [
                    {
                        "id": "provider/model-a",
                        "context_length": 999,
                        "top_provider": {"max_completion_tokens": 111},
                        "supported_parameters": ["reasoning", "tools"],
                    },
                    {
                        "id": "provider/local-model",
                        "context_length": 888,
                        "supported_parameters": ["reasoning"],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    base.write_text(
        json.dumps(
            {
                "version": 1,
                "generated_from": {"openrouter": "stale-openrouter.json", "litellm": "stale-litellm.json"},
                "models": {
                    "provider/model-a": {
                        "source": "openrouter:models:provider/model-a",
                        "context_window": 1,
                        "max_output_tokens": 2,
                        "reasoning": {
                            "enabled": True,
                            "param": "reasoning",
                            "efforts": ["stale"],
                        },
                    },
                    "provider/litellm-only": {
                        "source": "litellm:model_prices:provider/litellm-only",
                        "context_window": 1,
                        "supports_tools": False,
                    },
                    "provider/local-model": {
                        "context_window": 444,
                        "reasoning": {
                            "enabled": True,
                            "param": "reasoning_effort",
                            "efforts": ["low", "high"],
                        },
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--base",
            str(base),
            "--litellm",
            str(litellm),
            "--openrouter",
            str(openrouter),
            "--output",
            str(output),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    data = json.loads(output.read_text(encoding="utf-8"))
    model = data["models"]["provider/model-a"]
    assert model["source"] == "openrouter:models:provider/model-a"
    assert model["context_window"] == 999
    assert model["max_output_tokens"] == 111
    assert model["reasoning"]["efforts"] == ["none", "minimal", "low", "medium", "high", "xhigh"]

    litellm_only = data["models"]["provider/litellm-only"]
    assert litellm_only["source"] == "litellm:model_prices:provider/litellm-only"
    assert litellm_only["context_window"] == 333
    assert litellm_only["supports_tools"] is True

    local = data["models"]["provider/local-model"]
    assert local["context_window"] == 444
    assert local["reasoning"] == {
        "enabled": True,
        "param": "reasoning_effort",
        "efforts": ["low", "high"],
    }


def test_public_origin_catalog_keeps_model_centric_reasoning_aliases(tmp_path: Path):
    litellm = tmp_path / "model_prices_and_context_window.json"
    openrouter = tmp_path / "openrouter-models.json"
    catalog = tmp_path / "public-catalog.json"
    base = tmp_path / "model-capabilities.base.json"
    output = tmp_path / "model-capabilities.json"

    litellm.write_text(
        json.dumps(
            {
                "deepseek/deepseek-v4-pro": {
                    "mode": "chat",
                    "supports_reasoning": True,
                    "supports_max_reasoning_effort": True,
                }
            }
        ),
        encoding="utf-8",
    )
    openrouter.write_text(json.dumps({"data": []}), encoding="utf-8")
    catalog.write_text(
        json.dumps(
            {
                "data": [
                    {"id": "origin/ollama/deepseek-v4-pro"},
                    {"id": "origin/deepseek/deepseek-v4-pro"},
                    {"id": "deepseek-v4-pro"},
                ]
            }
        ),
        encoding="utf-8",
    )
    base.write_text('{"version":1,"models":{}}', encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--base",
            str(base),
            "--litellm",
            str(litellm),
            "--openrouter",
            str(openrouter),
            "--public-catalog",
            str(catalog),
            "--output",
            str(output),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["models"]["deepseek/deepseek-v4-pro"]["reasoning"]["efforts"] == ["max"]
