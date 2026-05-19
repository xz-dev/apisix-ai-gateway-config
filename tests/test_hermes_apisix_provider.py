"""Regression tests for the local Hermes APISIX ProviderProfile.

The desired design is:
1. Model discovery uses the gateway's unified OpenAI-compatible /v1/models
   catalog first, with Admin API pool-route introspection only as a fallback.
2. Every public model id maps to an ai-proxy-multi pool, even when that pool has
   a single outbound instance.
3. reasoning_effort capability is resolved from upstream provider APIs first.
4. The local capability registry is only a fallback when upstream metadata is
   unavailable or incomplete.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERMES_AGENT = Path.home() / ".hermes" / "hermes-agent"
if str(HERMES_AGENT) not in sys.path:
    sys.path.insert(0, str(HERMES_AGENT))


@pytest.fixture()
def apisix_profile():
    from providers import get_provider_profile

    profile = get_provider_profile("apisix")
    assert profile is not None, "apisix ProviderProfile must be registered"
    return profile


def test_apisix_profile_fetches_models_from_unified_gateway_catalog(apisix_profile):
    models = apisix_profile.fetch_models(api_key="unused-local-apisix", timeout=5.0)

    assert models is not None
    assert "ollama/glm-5.1" in models
    assert "deepseek/deepseek-v4-flash" in models
    assert "deepseek/deepseek-v4-pro" in models
    assert "siliconflow-cn/Qwen/Qwen3.6-35B-A3B" in models
    assert "xai/grok-4.3" in models
    assert len(models) >= 40


def test_reasoning_effort_prefers_upstream_provider_api_over_fallback_table(monkeypatch, apisix_profile):
    def fake_upstream(model, instance=None):
        assert model == "ollama/glm-5.1"
        return {
            "source": "upstream:test-provider-api",
            "supports_reasoning": True,
            "reasoning_efforts": ["xhigh"],
        }

    monkeypatch.setattr(apisix_profile, "_query_upstream_capability", fake_upstream)

    _, high_kwargs = apisix_profile.build_api_kwargs_extras(
        reasoning_config={"enabled": True, "effort": "high"},
        model="ollama/glm-5.1",
        base_url="http://127.0.0.1:4000/v1",
    )
    _, xhigh_kwargs = apisix_profile.build_api_kwargs_extras(
        reasoning_config={"enabled": True, "effort": "xhigh"},
        model="ollama/glm-5.1",
        base_url="http://127.0.0.1:4000/v1",
    )

    assert high_kwargs == {}, "fallback table says high is allowed, but upstream API must take precedence"
    assert xhigh_kwargs == {"reasoning_effort": "xhigh"}


def test_reasoning_effort_falls_back_to_registry_when_upstream_has_no_metadata(monkeypatch, apisix_profile):
    monkeypatch.setattr(apisix_profile, "_query_upstream_capability", lambda model, instance=None: None)

    extra_body, top_level = apisix_profile.build_api_kwargs_extras(
        reasoning_config={"enabled": True, "effort": "high"},
        model="ollama/glm-5.1",
        base_url="http://127.0.0.1:4000/v1",
    )

    assert extra_body == {}
    assert top_level == {"reasoning_effort": "high"}


def test_unsupported_fallback_effort_is_omitted(monkeypatch, apisix_profile):
    monkeypatch.setattr(apisix_profile, "_query_upstream_capability", lambda model, instance=None: None)

    _, top_level = apisix_profile.build_api_kwargs_extras(
        reasoning_config={"enabled": True, "effort": "minimal"},
        model="ollama/glm-5.1",
        base_url="http://127.0.0.1:4000/v1",
    )

    assert top_level == {}, "minimal must not be sent unless upstream API or fallback registry permits it"


def test_apisix_profile_does_not_send_reasoning_to_non_reasoning_models(monkeypatch, apisix_profile):
    monkeypatch.setattr(apisix_profile, "_query_upstream_capability", lambda model, instance=None: None)

    _, top_level = apisix_profile.build_api_kwargs_extras(
        reasoning_config={"enabled": True, "effort": "high"},
        model="siliconflow-cn/Qwen/Qwen3.6-35B-A3B",
        base_url="http://127.0.0.1:4000/v1",
    )

    assert top_level == {}
