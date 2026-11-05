# Jev domain router validation

Implemented on 3 November 2026. Production uses pinned `jev-1.14.0`, then the
existing LLM on errors or uncertainty. A valid uncertain Jev decision survives
LLM failure; total classifier failure exposes connected tools.

## Automated checks

- Full agent suite: **1,743 passed, 8 skipped**, plus 28 subtests. The skipped
  checks require optional integration environments. Existing LangSmith and
  Python-version deprecation warnings remain.
- The focused routing tests cover sync/async fallback, cancellation, cache reuse,
  typed-answer validation, connection retries, usage/pricing, pool shutdown,
  current and legacy snapshots, and concurrent-run isolation.
- A local LangSmith trace test verifies that raw queries and custom instructions
  are excluded at the Jev tracing boundary.
- Ruff checks for undefined/unused names and formatting pass for the new runtime,
  client, comparison harness, and their new tests. `git diff --check` passes.

## Evaluation corpus

`tests/data/domain_router_cases.json` contains 113 explicit expected decisions:
the original 98 historical queries and 15 added cases for mixed domains,
disconnected providers, custom defaults, explicit overrides, quoted content,
forwarded instructions, unsupported providers, and complexity.

Labels are reviewable expectations under the checked-in persona instructions;
they are not copied from the historical model decisions. In particular,
restaurant names remain informational and booking reminders route to Todoist.
Acknowledgements and cancelled requests use conversation; scheduling shorthand
uses the persona's routing defaults. Complexity labels remain subjective and
are reported separately through exact decision agreement.

Recomposing the recorded Jev 1.13 probabilities under the new reconciliation
policy produced 98 valid decisions, 73.47% exact agreement and 97.53% required
domain recall. Seven critical cases differ from their expected decisions.
This is **historical replay**, with changed policy, not a live Jev 1.14 result.
Latency, inference cost, and fallback frequency cannot be inferred from replay.

## Live acceptance remains pending

The TypeSafe credential is now configured in the ignored root and Docker agent
environment files. A live host smoke test returned `jev-1.14.0` in 482.6 ms, and
a native async smoke test inside the rebuilt agent container passed without LLM
fallback. The Compose stack was recreated and passed its health, resource, user,
and restart-history checks. The superseded agent image was removed; data volumes
were preserved.

The full live corpus comparison has not been performed, so the critical-case/recall
release acceptance gate remains unverified. A smoke test establishes connectivity
and runtime activation, not routing quality across the corpus.

After configuring the TypeSafe and selected LLM credentials, run:

```bash
venv/bin/python scripts/eval_domain_router.py --engine all --out /tmp/domain-router-comparison.json
```

The output compares Jev alone, the LLM baseline, and the cascade on identical
cases. It records exact agreement, required-domain recall, critical failures,
actual fallback calls, latency percentiles, returned models, usage, and cost.
`acceptance_passed` requires every critical cascade case to match and cascade
domain recall to meet or exceed the LLM baseline. The comparison exits with
status 1 when that gate fails. Output contains query text;
keep the artifact local.

For network-free historical inspection:

```bash
venv/bin/python scripts/eval_domain_router.py --engine replay --out /tmp/domain-router-replay.json
```

Rollback requires only `ROUTER_ENGINE=llm` and a process/container restart.
No database schema or checkpoint migration is involved.

## Image routing

Current-turn images and replayed clarification batches now pass through the
router instead of bypassing domain selection. Routing uses separate JPEG copies
rotated upright, scaled to at most 1568 pixels per side, and padded to at least
280 pixels per side. Previous-thread images stay model-only until recalled.
Original image bytes remain available to the vision orchestrator.

Jev receives multipart assets, each referenced once in state, with the
`TypeSafe-Danger-Zone-Images: true` request header. See the
[TypeSafe image contract](https://docs.typesafe.ai/images). Image support is
experimental upstream; the pinned `jev-1.14.0` model remains unchanged.

An image-bearing call gets exactly one attempt. Preparation errors, API errors,
invalid answers, or uncertainty immediately retry Jev without images, assets, or
the image header. This text-only call retains the configured retry policy, then
uses the existing LLM/candidate-domain/static fallback behavior. Cancellation
propagates without fallback. Missing Jev credentials or `ROUTER_ENGINE=llm`
uses text routing directly.

Run-local decision and failure caches include ordered image fingerprints and
batch boundaries. Diagnostics record counts and bounded reasons, never image
bytes or multipart bodies; detailed results retain only asset metadata. Every
successful response contributes usage, including uncertain or rejected answers.

Focused regression coverage:

```bash
venv/bin/python -m pytest tests/agents/test_router_images.py tests/agents/test_typesafe_runtime.py tests/agents/test_router_selector.py tests/agents/test_agent_node_router.py tests/agents/test_router_context_preservation.py
```
