# APISIX AI Gateway Config

This context defines the domain language for this repository's APISIX-based AI gateway configuration. It distinguishes public model identifiers, provider-origin routing, account-level load balancing, and alias fallback so routing behavior stays explicit and testable.

## Language

**Logical Provider**:
A provider family exposed by the gateway, such as `ollama`, `deepseek`, or `xai`. A logical provider can have one or more provider deployments behind it.
_Avoid_: account, deployment, upstream provider when referring to the grouped public provider name.

**Provider Deployment**:
One concrete credential/endpoint instance behind a logical provider, such as `ollama-1` or `ollama-2`. Provider deployments are not part of public model IDs; by default, deployments under the same logical provider are same-priority, equal-weight load-balancing targets.
_Avoid_: provider when referring to a single API key/account; fallback chain when referring to account-level deployment selection.

**Origin Model ID**:
A direct provider-origin model identifier in the form `origin/<logical-provider>/<raw-provider-model-id>`. The raw provider model ID begins after the logical provider segment and may itself contain `/` characters.
_Avoid_: provider-prefixed model ID, direct model string.

**Root Model ID**:
A client-facing model ID that does not start with `origin/`, preferably a provider-neutral model identifier such as `deepseek-v4-pro`. A root model ID resolves through model resolution rules; legacy provider-prefixed IDs such as `ollama/glm-5.1` are not created automatically because `ollama/` or `deepseek/` denotes provider origin, not a root alias namespace.
_Avoid_: provider-prefixed IDs like `deepseek/<model>` for root aliases; unqualified model when the model is actually an origin model.

**Model Resolution Rule**:
A root-namespace rule that maps a requested root model ID to one or more origin model references. Resolution rules may use exact IDs or regex templates with capture groups, and they are evaluated only for requests that do not start with `origin/`.
_Avoid_: provider route, catalog fallback.

**Failure Fallback Policy**:
A policy attached to a model resolution rule that allows APISIX to try the next origin model reference on approved runtime failures. The current APISIX-supported policy uses HTTP 429 and 5xx; timeout fallback is intentionally deferred while request timeouts remain bounded.
_Avoid_: catalog fallback, account load balancing, claiming timeout fallback without APISIX support.

**Catalog Fallback Models**:
Static provider model IDs used only when fetching a provider catalog fails or is unavailable. Catalog fallback models are not runtime model/provider fallback policy.
_Avoid_: fallback chain, failure fallback policy.

**Reasoning Capability**:
Model metadata that says whether a model supports reasoning and which reasoning strengths or effort values it accepts. Reasoning capability is treated as model-centric first, because provider catalogs such as Ollama or SiliconFlow may omit reasoning flags even when the underlying model supports them.
_Avoid_: assuming absence in a provider catalog means the model cannot reason.

## Example dialogue

Dev: "Should `ollama/glm-5.1` route directly to Ollama?"
Domain expert: "No. Direct provider routing must use the origin namespace: `origin/ollama/glm-5.1`. A model without `origin/` is a root model ID and must be resolved through an explicit model resolution rule."

Dev: "If we have two Ollama keys, do clients choose `ollama-1` or `ollama-2`?"
Domain expert: "No. Clients choose `origin/ollama/<model>` or a root model ID. The gateway binds provider deployments like `ollama-1` and `ollama-2` under the logical provider `ollama` and load-balances them internally."

Dev: "Can a root model fall back to any provider model with the same name?"
Domain expert: "No. Root fallback is only through configured origin model references, for example root `deepseek-v4-pro` may resolve to `origin/ollama/deepseek-v4-pro` and then `origin/deepseek/deepseek-v4-pro` if the rule says so."

Dev: "If I explicitly request `origin/ollama/deepseek-r1`, can it fall back to official DeepSeek on 429 or 5xx?"
Domain expert: "No. `origin/...` is provider-pinned. Cross-provider failure fallback is only for root namespace requests that match an approved model resolution rule; timeout fallback is not claimed in the current APISIX config."
