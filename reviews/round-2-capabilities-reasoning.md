# Review round 2: capabilities / reasoning metadata

Context note: `plan.md` and `progress.md` were not present in `/root/apisix-ai-gateway-config` during this review, so I reviewed the diff and relevant files directly.

## Blocker

- `scripts/render-routes.py` still lets provider-origin metadata with missing or weak reasoning fields mask model-centric reasoning metadata. The documented rule is that reasoning is model-centric and provider catalogs such as Ollama/SiliconFlow may omit reasoning flags (`CONTEXT.md:35-37`, `docs/adr/0001-origin-root-model-routing.md:3`, `docs/model-pools.md:167-169`). However, `capability_for_origin()` returns the first useful exact capability immediately (`scripts/render-routes.py:656-667`). That means an exact `ollama/deepseek-v4-pro` or `origin/siliconflow-cn/...` entry with context/tools but no `reasoning` prevents later raw/provider aliases with reasoning from being considered (`scripts/render-routes.py:668-676` is never reached).
  - DeepSeek/Ollama example reproduced: with `ollama/deepseek-v4-pro: {"context_window": 32000}` and `deepseek/deepseek-v4-pro` carrying `reasoning.efforts=["low","medium","high","xhigh","max"]`, the served payload for `origin/ollama/deepseek-v4-pro` was only `{"context_window": 32000}` while `origin/deepseek/...` and the root ID had reasoning.
  - Reasoning-effort example reproduced: with `ollama/deepseek-v4-pro` carrying `reasoning.enabled=true` but `efforts=[]`, and official DeepSeek metadata carrying the full efforts list, both `origin/ollama/deepseek-v4-pro` and root `deepseek-v4-pro` served `efforts: []`. This is because `has_reasoning()` only checks `enabled` (`scripts/render-routes.py:637-639`), and `capability_for_root()` returns the first target capability with `enabled=true` without checking whether effort/strength metadata is present (`scripts/render-routes.py:680-694`).
  - SiliconFlow example reproduced: `build-model-capabilities.py` can legitimately output both raw `Qwen/Qwen3.6-35B-A3B` reasoning metadata and an exact `origin/siliconflow-cn/Qwen/Qwen3.6-35B-A3B` OpenRouter/SiliconFlow fallback entry containing context/tools but no reasoning. The renderer then serves only the exact context/tools entry for `origin/siliconflow-cn/...`, dropping the raw model-centric reasoning.
  - Smallest safe fix: change origin/root capability selection from “return first useful exact entry” to “keep the first useful entry as the base, continue scanning exact aliases and suffix aliases for the best reasoning metadata, then merge missing/better `reasoning` into the base.” Treat `reasoning.enabled=true` with non-empty `efforts` as stronger than enabled with empty efforts, and enabled-with-empty as stronger than no reasoning. For roots, preserve the first useful target-order base but merge later target/raw reasoning efforts instead of replacing the whole root capability or stopping on empty efforts.

## Fix now

- Add regression coverage for the masking cases above. Existing tests cover the happy path where Ollama has no provider-specific capability entry (`tests/test_render_routes.py:262-304`, `tests/test_gateway_route_contract.py:228-273`) and where the build step keeps model-centric aliases (`tests/test_build_model_capabilities.py:462-519`), but they do not cover a provider-origin entry that exists and omits reasoning or has `efforts: []`. Add tests for:
  1. `ollama/deepseek-v4-pro` context-only + `deepseek/deepseek-v4-pro` reasoning => `origin/ollama/...` and root expose reasoning efforts.
  2. `ollama/deepseek-v4-pro` reasoning enabled with empty efforts + official/raw DeepSeek efforts => root and origin use the full efforts.
  3. SiliconFlow exact origin/provider entry without reasoning + raw `Qwen/...` reasoning => `origin/siliconflow-cn/...` exposes model-centric reasoning.

## Optional

- Current checked-in metadata is good for the specific sampled models: `conf/model-capabilities.json` has full DeepSeek reasoning efforts for `deepseek/deepseek-v4-pro` (`conf/model-capabilities.json:30-45`) and SiliconFlow Qwen reasoning efforts for `siliconflow-cn/Qwen/Qwen3.6-35B-A3B` (`conf/model-capabilities.json:274-290`). A current-conf render sample served reasoning for `origin/ollama/deepseek-v4-pro`, `origin/deepseek/deepseek-v4-pro`, root `deepseek-v4-pro`, and `origin/siliconflow-cn/Qwen/Qwen3.6-35B-A3B`.
- The build side is directionally correct: origin public IDs are expanded to provider-prefixed and raw source IDs (`scripts/build-model-capabilities.py:256-273`), and SiliconFlow OpenRouter aliasing handles both legacy `siliconflow-cn/...` and new `origin/siliconflow-cn/...` forms (`scripts/build-model-capabilities.py:276-287`).
- `/v1/model-capabilities` is filtered/mapped through the same generated catalog as `/v1/models` (`scripts/render-routes.py:697-717`), and validation rejects legacy provider-prefixed IDs in the served payload (`scripts/verify-gateway.py:260-263`).

## Defer

- No source changes were made in this review. The only written file is this review artifact.

## Commands run

- `git status --short && git diff --stat` — exit 0.
- `python3 -m pytest tests/test_build_model_capabilities.py tests/test_render_routes.py tests/test_gateway_route_contract.py` — exit 1 (`No module named pytest`; tests could not be run in this environment).
- `python3 -m py_compile scripts/build-model-capabilities.py scripts/render-routes.py scripts/verify-gateway.py` — exit 0.
- Custom render reproduction: Ollama exact context-only metadata masking DeepSeek model-centric reasoning — exit 0; reproduced missing reasoning on `origin/ollama/deepseek-v4-pro`.
- Custom render reproduction: Ollama `reasoning.enabled=true` with `efforts=[]` masking official DeepSeek efforts — exit 0; reproduced empty efforts on origin and root.
- Custom render reproduction: SiliconFlow exact context/tools metadata masking raw Qwen reasoning — exit 0; reproduced missing reasoning on `origin/siliconflow-cn/Qwen/Qwen3.6-35B-A3B`.
- Custom build reproduction: LiteLLM raw Qwen reasoning plus OpenRouter/SiliconFlow context-only fallback — exit 0; build output contained both raw reasoning and exact origin context-only entries, confirming the renderer needs to merge/prefer reasoning.
- Current-conf render sample for DeepSeek/Ollama/SiliconFlow reasoning — exit 0; sampled checked-in metadata served reasoning correctly when no weaker exact entry was present.
