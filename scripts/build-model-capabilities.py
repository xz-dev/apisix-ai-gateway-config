#!/usr/bin/env python3
"""Build APISIX model-capabilities.json from a LiteLLM table plus local overrides.

The raw LiteLLM model_prices_and_context_window.json is an upstream input, not a
runtime dependency for the Hermes APISIX provider. This script normalizes the
LiteLLM shape into APISIX's capability registry shape, overlays local operator
entries, and writes the final conf/model-capabilities.json that APISIX publishes
through /v1/model-capabilities.
"""

from __future__ import annotations

import argparse
import json
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_LITELLM_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
EFFORT_FLAG_MAP = {
    "supports_none_reasoning_effort": "none",
    "supports_minimal_reasoning_effort": "minimal",
    "supports_low_reasoning_effort": "low",
    "supports_xhigh_reasoning_effort": "xhigh",
    "supports_max_reasoning_effort": "max",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="Local APISIX capability override JSON")
    parser.add_argument("--litellm", default=DEFAULT_LITELLM_URL, help="LiteLLM model_prices JSON URL or local file")
    parser.add_argument("--output", required=True, help="Final merged model-capabilities.json path")
    parser.add_argument(
        "--only-model",
        action="append",
        default=[],
        help="Optional public model id to include from LiteLLM conversion; repeatable. Local base overrides are always kept.",
    )
    return parser.parse_args()


def load_json(path_or_url: str) -> dict[str, Any]:
    if path_or_url.startswith(("http://", "https://")):
        with urllib.request.urlopen(path_or_url, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    else:
        data = json.loads(Path(path_or_url).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"expected JSON object: {path_or_url}")
    return data


def intish(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None


def efforts_from_litellm(spec: dict[str, Any]) -> list[str]:
    efforts = [effort for key, effort in EFFORT_FLAG_MAP.items() if spec.get(key) is True]
    # LiteLLM often only stores supports_reasoning=true and no exact strength
    # information (for example xai/grok-4.3). Do not invent low/medium/high here;
    # local APISIX overrides can supply exact efforts when the provider docs/API
    # make them known.
    return efforts


def convert_litellm_entry(model_id: str, spec: dict[str, Any]) -> dict[str, Any] | None:
    if spec.get("mode") not in (None, "chat", "completion"):
        return None

    capability: dict[str, Any] = {"source": f"litellm:model_prices:{model_id}"}
    context_window = intish(spec.get("max_input_tokens") or spec.get("context_window") or spec.get("max_tokens"))
    max_output_tokens = intish(spec.get("max_output_tokens"))
    if context_window is not None:
        capability["context_window"] = context_window
    if max_output_tokens is not None:
        capability["max_output_tokens"] = max_output_tokens

    if any(key in spec for key in ("supports_function_calling", "supports_tool_choice", "supports_parallel_function_calling")):
        capability["supports_tools"] = bool(
            spec.get("supports_function_calling")
            or spec.get("supports_tool_choice")
            or spec.get("supports_parallel_function_calling")
        )
    if any(key in spec for key in ("supports_vision", "supports_pdf_input")):
        capability["supports_vision"] = bool(spec.get("supports_vision") or spec.get("supports_pdf_input"))

    if spec.get("supports_reasoning") is True or any(key in spec for key in EFFORT_FLAG_MAP):
        capability["reasoning"] = {
            "enabled": spec.get("supports_reasoning") is not False,
            "param": "reasoning_effort",
            "efforts": efforts_from_litellm(spec),
        }

    return capability if len(capability) > 1 else None


def converted_litellm_models(table: dict[str, Any], only_models: set[str]) -> dict[str, Any]:
    converted: dict[str, Any] = {}
    for model_id, spec in table.items():
        if only_models and model_id not in only_models:
            continue
        if not isinstance(spec, dict):
            continue
        cap = convert_litellm_entry(model_id, spec)
        if cap:
            converted[model_id] = cap
    return converted


def main() -> int:
    args = parse_args()
    base = load_json(args.base)
    table = load_json(args.litellm)
    raw_base_models = base.get("models")
    base_models: dict[str, Any] = raw_base_models if isinstance(raw_base_models, dict) else {}
    only_models = set(args.only_model or [])
    models = converted_litellm_models(table, only_models)
    # Local APISIX entries are authoritative: they can add new models absent from
    # LiteLLM and can override exact reasoning efforts for models where LiteLLM
    # only has supports_reasoning=true.
    models.update({key: value for key, value in base_models.items() if isinstance(value, dict)})

    output = {
        "version": base.get("version") or 1,
        "description": base.get("description")
        or "APISIX capability registry generated from LiteLLM metadata plus local overrides.",
        "generated_from": {
            "litellm": args.litellm,
            "base": args.base,
            "local_overrides_win": True,
        },
        "models": dict(sorted(models.items(), key=lambda item: item[0].lower())),
    }
    Path(args.output).write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": args.output, "model_count": len(models)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
