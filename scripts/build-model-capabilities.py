#!/usr/bin/env python3
"""Build APISIX model-capabilities.json from upstream metadata plus local overrides.

The raw LiteLLM model_prices_and_context_window.json is an upstream input, not a
runtime dependency for the Hermes APISIX provider. This script normalizes the
LiteLLM shape into APISIX's capability registry shape, optionally overlays
OpenRouter provider metadata, overlays local operator entries, and writes the
final conf/model-capabilities.json that APISIX publishes through
/v1/model-capabilities.
"""

from __future__ import annotations

import argparse
import json
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_LITELLM_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
DEFAULT_OPENROUTER_URL = "https://openrouter.ai/api/v1/models"
OPENROUTER_REASONING_EFFORTS = ["none", "minimal", "low", "medium", "high", "xhigh"]
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
    parser.add_argument(
        "--openrouter",
        default=None,
        nargs="?",
        const=DEFAULT_OPENROUTER_URL,
        help=f"Optional OpenRouter models JSON URL or local file (default endpoint: {DEFAULT_OPENROUTER_URL})",
    )
    parser.add_argument("--output", required=True, help="Final merged model-capabilities.json path")
    parser.add_argument(
        "--public-catalog",
        default=None,
        help="Optional public /v1/models-style catalog used for provider-specific OpenRouter fallback aliases",
    )
    parser.add_argument(
        "--only-model",
        action="append",
        default=[],
        help="Optional public model id to include from upstream conversion; repeatable. Local base overrides are always kept.",
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


def merge_model_overlay(models: dict[str, Any], overlay: dict[str, Any]) -> None:
    for model_id, capability in overlay.items():
        existing = models.get(model_id)
        if isinstance(existing, dict):
            merged = dict(existing)
            merged.update(capability)
            models[model_id] = merged
        else:
            models[model_id] = capability


def is_generated_provider_source(source: Any) -> bool:
    return isinstance(source, str) and source.startswith(("litellm:", "openrouter:"))


def local_override_models(base_models: dict[str, Any]) -> dict[str, Any]:
    """Return only operator-authored model entries from a base registry.

    Generated registries can be fed back as --base by configure-routes.sh. In
    that mode, LiteLLM/OpenRouter entries from the previous run must not become
    authoritative local overrides over fresher upstream metadata. Entries with no
    source (the historical local override shape) or with a non-generated custom
    source remain local overrides.
    """
    overrides: dict[str, Any] = {}
    for model_id, capability in base_models.items():
        if not isinstance(capability, dict):
            continue
        if is_generated_provider_source(capability.get("source")):
            continue
        overrides[model_id] = capability
    return overrides


def convert_openrouter_entry(entry: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    model_id = entry.get("id")
    if not isinstance(model_id, str) or not model_id:
        return None

    capability: dict[str, Any] = {"source": f"openrouter:models:{model_id}"}
    context_window = intish(entry.get("context_length"))
    top_provider = entry.get("top_provider")
    max_output_tokens = None
    if isinstance(top_provider, dict):
        max_output_tokens = intish(top_provider.get("max_completion_tokens"))

    if context_window is not None:
        capability["context_window"] = context_window
    if max_output_tokens is not None:
        capability["max_output_tokens"] = max_output_tokens

    supported_parameters = entry.get("supported_parameters")
    if not isinstance(supported_parameters, list):
        supported_parameters = []
    parameter_set = {value for value in supported_parameters if isinstance(value, str)}

    if "supports_tools" in entry:
        capability["supports_tools"] = bool(entry.get("supports_tools"))
    elif parameter_set.intersection({"tools", "tool_choice"}):
        capability["supports_tools"] = True

    input_modalities: set[str] = set()
    architecture = entry.get("architecture")
    if isinstance(architecture, dict):
        raw_modalities = architecture.get("input_modalities")
        if isinstance(raw_modalities, list):
            input_modalities = {value for value in raw_modalities if isinstance(value, str)}
    if "supports_vision" in entry:
        capability["supports_vision"] = bool(entry.get("supports_vision"))
    elif input_modalities.intersection({"image", "file"}):
        capability["supports_vision"] = True

    if parameter_set.intersection({"reasoning", "reasoning_effort"}):
        capability["reasoning"] = {
            "enabled": True,
            "param": "reasoning",
            "efforts": OPENROUTER_REASONING_EFFORTS,
        }

    return (model_id, capability) if len(capability) > 1 else None


def converted_openrouter_models(payload: dict[str, Any], only_models: set[str]) -> dict[str, Any]:
    raw_entries = payload.get("data")
    if not isinstance(raw_entries, list):
        raise SystemExit("expected OpenRouter models JSON object with data[]")

    converted: dict[str, Any] = {}
    for entry in raw_entries:
        if not isinstance(entry, dict):
            continue
        converted_entry = convert_openrouter_entry(entry)
        if not converted_entry:
            continue
        model_id, capability = converted_entry
        if only_models and model_id not in only_models:
            continue
        converted[model_id] = capability
    return converted


def public_catalog_model_ids(payload: dict[str, Any]) -> list[str]:
    raw_entries = payload.get("data")
    if not isinstance(raw_entries, list):
        raise SystemExit("expected public catalog JSON object with data[]")
    model_ids: list[str] = []
    for entry in raw_entries:
        if not isinstance(entry, dict):
            continue
        model_id = entry.get("id")
        if isinstance(model_id, str) and model_id:
            model_ids.append(model_id)
    return model_ids


def siliconflow_alias_for_openrouter(openrouter_model_id: str, public_model_ids: list[str]) -> str | None:
    openrouter_lower = openrouter_model_id.lower()
    for public_model_id in public_model_ids:
        prefix = "siliconflow-cn/"
        if not public_model_id.lower().startswith(prefix):
            continue
        if public_model_id[len(prefix) :].lower() == openrouter_lower:
            return public_model_id
    return None


def openrouter_fallback_capability(capability: dict[str, Any]) -> dict[str, Any]:
    fallback = dict(capability)
    reasoning = fallback.get("reasoning")
    if isinstance(reasoning, dict):
        fallback["reasoning"] = {
            "enabled": True,
            "param": "reasoning_effort",
            "efforts": ["minimal", "low", "medium", "high", "xhigh"],
        }
    return fallback


def converted_openrouter_siliconflow_fallbacks(
    openrouter_models: dict[str, Any], public_model_ids: list[str], existing_models: dict[str, Any], only_models: set[str]
) -> dict[str, Any]:
    converted: dict[str, Any] = {}
    for openrouter_model_id, capability in openrouter_models.items():
        public_model_id = siliconflow_alias_for_openrouter(openrouter_model_id, public_model_ids)
        if not public_model_id:
            continue
        if only_models and public_model_id not in only_models:
            continue
        converted[public_model_id] = openrouter_fallback_capability(capability)
    return converted


def main() -> int:
    args = parse_args()
    base = load_json(args.base)
    table = load_json(args.litellm)
    raw_base_models = base.get("models")
    base_models: dict[str, Any] = raw_base_models if isinstance(raw_base_models, dict) else {}
    only_models = set(args.only_model or [])
    public_model_ids: list[str] | None = None
    if args.public_catalog:
        catalog_payload = load_json(args.public_catalog)
        public_model_ids = public_catalog_model_ids(catalog_payload)
        if not only_models:
            only_models = set(public_model_ids)
    models = converted_litellm_models(table, only_models)
    openrouter_siliconflow_fallback_count = 0
    if args.openrouter:
        openrouter_payload = load_json(args.openrouter)
        openrouter_models = converted_openrouter_models(openrouter_payload, only_models)
        if args.public_catalog:
            fallback_models = converted_openrouter_siliconflow_fallbacks(
                converted_openrouter_models(openrouter_payload, set()),
                public_model_ids or [],
                models,
                only_models,
            )
            openrouter_siliconflow_fallback_count = len(fallback_models)
            merge_model_overlay(models, fallback_models)
        else:
            merge_model_overlay(models, openrouter_models)
    # Local APISIX entries are authoritative: they can add new models absent from
    # upstream metadata and can override exact reasoning efforts for models where
    # provider metadata only exposes generic reasoning support. When a generated
    # registry is reused as --base, ignore generated provider entries so stale
    # LiteLLM/OpenRouter data cannot override fresh upstream metadata.
    models.update(local_override_models(base_models))

    output = {
        "version": base.get("version") or 1,
        "description": base.get("description")
        or "APISIX capability registry generated from LiteLLM/OpenRouter metadata plus local overrides.",
        "generated_from": {
            "litellm": args.litellm,
            "openrouter": args.openrouter,
            "public_catalog": args.public_catalog,
            "openrouter_siliconflow_fallback_count": openrouter_siliconflow_fallback_count,
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
