# Hardened Plan: User-Specific Runtime Configuration

## Summary

The proposal has the right foundation—reuse `user_preferences` JSONB, resolve configuration in Python, and avoid a new table—but the current plan is not implementation-ready.

Today, model selection, reasoning effort, maximum turns, and mutation permission originate from process-wide environment settings loaded through `config.py` and `constants.py`. The existing request path already resolves `user_preferences` into `RuntimeContextSnapshot.preferences` before `builder.py` constructs `RunDeps`, so the per-request dependency-injection seam exists and should be extended rather than replaced. No TypeScript-to-Python configuration payload is needed.

## Implementation Audit (2026-08-02, re-verified against source)

Status legend: **complete** means implemented locally and covered by a passing focused test; **authored** means the change exists locally but still needs database validation; **partial** means only part of the stage exists; **not started** means no implementation was found.

| Stage | Status | Evidence and remaining gate |
| --- | --- | --- |
| 1. Preference models and resolver | **Complete locally** | `LlmPreferences`, `ExecutionPreferences`, `ResolvedUserRuntimeConfig`, the pure resolver, default factories (`preferences.py:203-297`, `AssistantPreferencesV1:225-226`), and package exports (`user_context/__init__.py`) are present. `tests/agents/test_llm_preferences.py` + `test_runtime_config_wiring.py` pass all 25 cases under Python 3.13. |
| 2. Supabase validation migration | **Deployed and remotely verified** | `supabase/migrations/20260802091219_extend_user_runtime_preferences_v1.sql` adds the two helper validators, replaces the V1 validator without dropping prior rules, retains revokes/grants, and revalidates the existing constraint. The SQL whitespace predicate was aligned with Pydantic before deployment. Remote verification confirmed the migration record, function properties and privileges, validated constraint, all 15 existing V1 rows, and the valid/invalid boundary matrix. Local database reset and SQL integration tests remain outstanding. |
| 3. Request and graph wiring | **Nearly complete — one gap** | Routes preserve optional `allow_mutations`/`max_agent_turns` as `None` (`invoke.py`, `resume.py`, `schemas.py`). `builder.py:571-587` calls `resolve_user_runtime_config` once (offline path uses `None` prefs), feeds `resolved.allow_mutations` to `ToolDispatcher` and `resolved.max_agent_turns` + forced LLM fields to `RunDeps` (`run_deps.py:31-33`). Orchestrator applies user pins after every router branch and on the no-decision path (`orchestrator.py:1102-1117`). Dispatcher blocks mutating execution when read-only. **Remaining gap:** selectors do not exclude mutating schemas in read-only mode — `StaticToolSelector` and `RouterToolSelector` swallow `allow_mutations` and `KeywordToolSelector` honors it only in its matched branch, not the fallback branch. Manual acceptance #3's "mutating tools are excluded" half is unmet (execution is still blocked by the dispatcher, so runtime safety holds). |
| 4. Telemetry and cost attribution | **Not started** | `UsageSummary` records tokens only, completion calls do not record the actual model, and post-run persistence still writes `DEEPSEEK_MODEL`. Mixed-model and unknown-model behavior is not implemented or tested. |
| 5. Fixtures, tests, and administration | **Partial** | `make_preferences()` supports `llm` and `execution`, and schema/resolver tests exist. The admin CLI already validates and serializes the new sections generically, including explicit `null`, but set/clear documentation and tests are absent. Wiring, router, isolation, resume, telemetry, and SQL integration coverage are also missing. |
| 6. Local validation | **Blocked / incomplete** | `git diff --check` and `npm run build` pass. The checked-in `venv` has a stale interpreter path, Docker is not running, three DSN-gated database integration tests skip, and broader targeted Python runs have an existing admin-CLI serialization assertion failure (`test_preferences_are_strictly_validated`). No `db:reset`, DB lint/migration check, full Python suite, load test, or advisors have completed for this change. |
| 7. Production rollout | **Migration deployed; application deployment pending** | Remote migration `20260802091219_extend_user_runtime_preferences_v1` is applied and verified. Application code has not been deployed, and no user row contains the new `llm` or `execution` values as part of this rollout. |

### Remaining implementation checklist

- [x] Preserve optional request restrictions through invoke, streaming invoke, resume, streaming resume, and bulk paths. Change runner inputs to optional request overrides and resolve them only after fresh or snapshotted preferences are available.
- [x] Resolve the effective runtime configuration exactly once in `builder.py`, including the offline path, before constructing request-local dispatch, selection, routing, and graph dependencies.
- [x] Add `forced_model` and `forced_reasoning_effort` to `RunDeps`; apply each non-null pin after every `ModelRouter` decision and on the no-decision path.
- [x] Feed `resolved.allow_mutations` to `ToolDispatcher` (execution guard) and `resolved.max_agent_turns` to `RunDeps`. Note: `allow_mutations` is passed to the selector factory but is not yet honored by the selectors themselves (see next item).
- [ ] Make every selector honor read-only mode when returning schemas. The dispatcher already blocks execution, but current static, router, and keyword-fallback paths can still expose mutating tools, which does not satisfy the manual acceptance criterion. **Still outstanding:** filter mutating schemas in `StaticToolSelector` (`static.py`), the fallback branch of `KeywordToolSelector` (`keyword.py:120-121`), and `RouterToolSelector` (`router.py`) when `allow_mutations=False`.
- [ ] Update async run-log/tracing metadata to report effective safety values and whether model/reasoning are pinned. Do not label the process default as the final model used.
- [ ] Extend run-scoped usage accounting with successful-completion model identities. Persist the sole actual model, or `mixed` with `cost_usd = NULL`; preserve `NULL` cost for an unknown single model. Explicitly decide whether this scope includes router/summarizer completions or only orchestrator agent completions, then make implementation, tests, and documentation agree.
- [x] Fix the migration's `llm.model` whitespace predicate so SQL rejects leading/trailing and whitespace-only values with the same semantics as Pydantic; verify tab/newline cases remotely.
- [ ] Complete the schema/resolver matrix with float/string/bool and negative turn values, explicit `null`/empty sections, request-side `false`, user-side `true`/`None`, simultaneous user/request clamps, and offline request restrictions.
- [ ] Add focused route/builder/orchestrator tests for default behavior, global/user/request precedence, all router branches, no-decision routing, offline behavior, concurrent-user isolation, and snapshot-frozen resume behavior with live restrictions.
- [ ] Add telemetry tests for actual-model attribution, Pro pricing, unknown pricing, and mixed-model runs.
- [ ] Add SQL integration coverage for old payloads, valid sections, malformed values and extra keys, revision increments, and validation of all existing V1 rows.
- [ ] Add administrative CLI documentation/tests showing how to set and clear every new field while preserving the full validated preference document.
- [ ] Repair or recreate the Python virtual environment and resolve the existing admin-CLI test failure before using the full suite as a rollout gate.
- [ ] Start Docker and complete the local Supabase and application validation commands listed below.

### Ordered rollout gates

1. Finish Stages 3–5 and obtain a clean local diff/test result.
2. Run `npm run db:reset`, SQL integration tests, `npm run db:lint`, and `npm run db:migrations`; verify all stored V1 rows satisfy the replaced validator.
3. Run targeted Python suites, then `venv/bin/pytest tests/agents -x`, followed by `npm run build`. Run the 12-user load test as the separate capacity/isolation gate.
4. Run Supabase security and performance advisors against the migrated schema and review any findings.
5. Deploy only the Supabase migration; verify migration history, the constraint, helper privileges, existing-row validity, and a valid/invalid write smoke test.
6. Deploy the application code; verify default users are unchanged and effective configuration/actual-model telemetry is correct.
7. Only after both deployments are healthy, add `llm` or `execution` values to user rows, starting with controlled canary users and the manual acceptance cases below.

Required corrections:

- Add a Supabase migration; the database has a V1 JSON validator that must understand and validate the new sections.
- Split LLM choices from execution controls.
- Define safe precedence instead of “user values always win.”
- Pin user model/reasoning overrides after `ModelRouter` selection.
- Record the actual model used so Pro traffic is not logged and priced as Flash.
- Expand verification beyond one overlay test.

No new table, RLS policy, or JSONB index is needed. Runtime reads by the existing `user_id` primary key rather than filtering on JSON properties. Existing RLS and least-privilege backend access remain unchanged, consistent with [Supabase RLS guidance](https://supabase.com/docs/guides/database/postgres/row-level-security). The 2026 Data API grant change does not affect this direct-Postgres path. [Supabase changelog](https://supabase.com/changelog/45329-breaking-change-tables-not-exposed-to-data-and-graphql-api-automatically)

## Preference and Database Contract

Extend `AssistantPreferencesV1` in `agents/agent_api/app/user_context/preferences.py` with two optional, default-empty sections:

```json
{
  "llm": {
    "model": "deepseek-v4-pro",
    "reasoning_effort": "max"
  },
  "execution": {
    "max_agent_turns": 20,
    "allow_mutations": false
  }
}
```

Add strict Pydantic models before `AssistantPreferencesV1`:

Import `StrictBool` from Pydantic alongside the existing model primitives.

```python
class LlmPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    model: Optional[str] = Field(default=None, min_length=1, max_length=100)
    reasoning_effort: Optional[Literal["off", "low", "high", "max"]] = None


class ExecutionPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_agent_turns: Optional[int] = Field(
        default=None,
        strict=True,
        gt=0,
        le=50,
    )
    allow_mutations: Optional[StrictBool] = None


class AssistantPreferencesV1(BaseModel):
    # Existing fields remain unchanged.
    llm: LlmPreferences = Field(default_factory=LlmPreferences)
    execution: ExecutionPreferences = Field(default_factory=ExecutionPreferences)
```

Use Pydantic string normalization so whitespace-only model names are rejected and stored model identifiers are trimmed. The default factories ensure existing database rows parse with all-`None` overrides.

- `llm.model`: trimmed, non-empty string, maximum 100 characters.
- `llm.reasoning_effort`: `off | low | high | max`.
- `execution.max_agent_turns`: integer from 1 through 50.
- `execution.allow_mutations`: boolean.
- Reject unknown keys inside both sections.
- Omitted sections and `null` fields preserve current system behavior.
- Keep `schema_version = 1`; this is a backward-compatible optional extension with no backfill.

Create the migration through `npx --no-install supabase migration new extend_user_runtime_preferences_v1`. Update `private.is_valid_user_preferences_v1` and add defensive `SECURITY INVOKER` helper validation for the two sections, retaining the existing revokes and narrow execution grants. Verify all existing rows still satisfy the validator.

Do not add a column, table, GIN index, Data API grant, or new RLS policy.

## Runtime Resolution and Interfaces

Introduce one immutable resolved runtime-config object and calculate it after loading the fresh preference row or stored resume snapshot.

- Model and reasoning:
  - A non-null user value is a forced override applied after every `ModelRouter` branch.
  - Unspecified fields retain existing dynamic router/client behavior.
  - Forced values must also apply when there is no router decision.
- Mutation permission:
  - `effective = global_setting AND user_setting AND request_setting`.
  - Missing user/request values count as permissive; they cannot re-enable a higher-level `false`.
- Turn budget:
  - `effective = min(global_max, user_max if present, request_max if present)`.
  - `JARVIS_MAX_AGENT_TURNS` remains the global safety ceiling.
- Use explicit `is None` checks; do not use truthiness-based `or` for boolean or numeric configuration.
- FastAPI request shapes remain unchanged, but routes pass optional request overrides through without converting them to process defaults prematurely.
- Offline/test runs without a database use global settings plus any request-side restriction.

Store both sections in `RuntimeContextSnapshot`. Per the selected behavior, resumes use the thread’s stored configuration even if the database row has since changed; live global and request restrictions still apply. Document that administrators must cancel or expire a paused thread if a database-only read-only change must affect it immediately.

Implement the resolution and per-run wiring around `agents/agent_api/app/graph/builder.py` and `RunDeps`, preserving shared SDK/database connection pools and concurrent-user isolation.

### Resolved configuration

Add an immutable `ResolvedUserRuntimeConfig` and one pure resolver rather than returning an unlabelled tuple:

```python
@dataclass(frozen=True)
class ResolvedUserRuntimeConfig:
    forced_model: Optional[str]
    forced_reasoning_effort: Optional[str]
    max_agent_turns: int
    allow_mutations: bool


def resolve_user_runtime_config(
    *,
    global_max_turns: int,
    global_allow_mutations: bool,
    llm: Optional[LlmPreferences],
    execution: Optional[ExecutionPreferences],
    request_max_turns: Optional[int],
    request_allow_mutations: Optional[bool],
) -> ResolvedUserRuntimeConfig:
    user_max = (
        execution.max_agent_turns
        if execution is not None and execution.max_agent_turns is not None
        else global_max_turns
    )
    request_max = (
        request_max_turns
        if request_max_turns is not None
        else global_max_turns
    )
    return ResolvedUserRuntimeConfig(
        forced_model=llm.model if llm is not None else None,
        forced_reasoning_effort=(
            llm.reasoning_effort if llm is not None else None
        ),
        max_agent_turns=min(global_max_turns, user_max, request_max),
        allow_mutations=(
            global_allow_mutations
            and not (
                execution is not None
                and execution.allow_mutations is False
            )
            and request_allow_mutations is not False
        ),
    )
```

The production implementation may place this resolver beside the preference models or in a focused runtime-configuration module, but it must remain pure, typed, and independently unit-testable.

### Builder and graph wiring

1. Change the invoke, streaming invoke, resume, streaming resume, and bulk paths to pass optional request overrides into `run_jarvis_async` without first replacing `None` with process defaults.
2. After `runtime_context` is resolved in `builder.py`, obtain `llm` and `execution` from `runtime_context.snapshot.preferences`; use empty preference objects for the offline path.
3. Call `resolve_user_runtime_config` before constructing `ToolDispatcher`, the tool selector, `ModelRouter`, and `RunDeps`.
4. Construct `ToolDispatcher` and selectors with `resolved.allow_mutations`, and set `RunDeps.max_agent_turns` to `resolved.max_agent_turns`.
5. Add `forced_model` and `forced_reasoning_effort` to `RunDeps`. In the orchestrator node, first obtain any dynamic `ModelRouter` selection, then replace only the fields explicitly forced by the user. Apply forced fields even when no router decision exists.
6. Keep `create_default_model_router` configured from the existing system settings. A user override clamps the selected field across default, complex, and multi-domain branches; an absent override leaves routing unchanged.
7. Record the resolved safety values and whether model/reasoning are pinned in run metadata. Do not claim a final model until the completion call records the model actually used.

No graph topology change is required. `ToolDispatcher` and `RunDeps` already accept request-local mutation and turn settings; this work changes how their final values are calculated.

## Telemetry and Administration

- Extend the run-scoped usage accumulator to record the model used by each successful completion.
- If one model served the run, persist that exact model and calculate cost using it.
- If multiple models unexpectedly served one run, persist `model = "mixed"` and `cost_usd = NULL` rather than recording a misleading cost.
- Trace the resolved model/reasoning, effective turn cap, and mutation permission through existing async tracing/run-log facilities; do not add a new logging sink.
- Keep arbitrary bounded model IDs so an administrator can test preview models. Unknown pricing continues to produce `NULL` cost.
- The existing administrative preferences CLI remains the write path and gains the new validation automatically; include examples for setting and clearing both sections.

Administrative examples:

```json
{
  "llm": {
    "model": "deepseek-v4-pro",
    "reasoning_effort": "max"
  },
  "execution": {
    "max_agent_turns": 20,
    "allow_mutations": false
  }
}
```

Clearing a field means removing it or setting it to `null`; the administrative CLI's validated serialization should preserve the existing behavior for omitted values. The full preferences document still passes through `private.admin_set_preferences`, increments `revision`, and produces the existing `preferences_updated` audit event.

## Detailed Implementation Stages

### Stage 1: Preference models and resolver — complete locally

- Add `LlmPreferences`, `ExecutionPreferences`, `ResolvedUserRuntimeConfig`, and the pure resolver.
- Add default factories to `AssistantPreferencesV1`.
- Export the new public preference/runtime types through the existing `user_context` package surface where needed.

### Stage 2: Supabase validation migration — deployed and remotely verified

- Generate the migration with the repository-pinned Supabase CLI.
- Add strict database validation for both optional objects and their allowed keys, types, enums, string bounds, and integer bounds.
- Replace `private.is_valid_user_preferences_v1` so the existing table constraint enforces the extended contract.
- Reapply the established function revokes/grants and verify all stored V1 rows.

### Stage 3: Request and graph wiring — nearly complete (one gap)

- [x] Preserve `None` on inbound request overrides until the preference row or resume snapshot is available.
- [x] Resolve effective values once per invocation.
- [x] Wire the resolved mutation flag (dispatcher execution guard), turn limit, and forced LLM fields into request-local dependencies.
- [x] Apply model/reasoning pins after dynamic routing without rebuilding shared transports.
- [ ] Exclude mutating schemas in the selectors when read-only (static/router/keyword-fallback still expose them).

### Stage 4: Telemetry and cost attribution — not started

- Associate successful completion usage with its actual model.
- Use the recorded model for durable usage logging and pricing.
- Retain safe fallback behavior for unknown or mixed models.

### Stage 5: Test fixtures and administration — partial

Update `tests/agents/runtime_helpers.py` so `make_preferences()` accepts independent optional `llm` and `execution` dictionaries and includes them only when supplied:

```python
def make_preferences(
    ...,
    llm: Optional[Dict[str, object]] = None,
    execution: Optional[Dict[str, object]] = None,
) -> AssistantPreferencesV1:
    return AssistantPreferencesV1.model_validate({
        ...,
        **({"llm": llm} if llm is not None else {}),
        **({"execution": execution} if execution is not None else {}),
    })
```

- Add examples to the administrative workflow documentation or CLI tests.
- Keep existing fixtures unchanged unless a test explicitly exercises an override.

## Test and Rollout Plan

- Pydantic tests: omitted sections, valid values, empty/oversized models, invalid reasoning, non-integer/out-of-range turns, incorrect booleans, and unknown keys.
- SQL integration tests: existing payloads remain valid; valid new sections persist; malformed sections are rejected by the table constraint; preference revision increments.
- Resolution tests: every global/user/request precedence combination, especially explicit `false`, global turn ceilings, and offline defaults.
- Router tests: pinned model/reasoning across low, complex, multi-domain, and no-decision paths; unspecified fields remain dynamic.
- Isolation tests: two concurrent users receive different models, turn caps, and mutation permissions without cross-run leakage.
- Resume tests: the saved configuration is reused, while a live global/request restriction can still disable mutations or lower the turn cap.
- Telemetry tests: pinned Pro is logged and priced as Pro; unknown models have null cost; mixed-model runs do not receive fabricated pricing.
- Add `tests/agents/test_llm_preferences.py` with at least these named cases:
  - `TestLlmPreferencesSchema.test_defaults_to_all_none`
  - `TestLlmPreferencesSchema.test_accepts_valid_values`
  - `TestLlmPreferencesSchema.test_rejects_invalid_reasoning_effort`
  - `TestLlmPreferencesSchema.test_rejects_empty_or_oversized_model`
  - `TestLlmPreferencesSchema.test_rejects_extra_fields`
  - `TestExecutionPreferencesSchema.test_defaults_to_all_none`
  - `TestExecutionPreferencesSchema.test_rejects_zero_turns`
  - `TestExecutionPreferencesSchema.test_rejects_turns_over_50`
  - `TestExecutionPreferencesSchema.test_rejects_non_boolean_mutation_value`
  - `TestAssistantPreferencesRuntimeSections.test_existing_payload_gets_empty_defaults`
  - `TestResolveUserRuntimeConfig.test_none_preferences_return_system_behavior`
  - `TestResolveUserRuntimeConfig.test_model_override_only`
  - `TestResolveUserRuntimeConfig.test_reasoning_override_only`
  - `TestResolveUserRuntimeConfig.test_user_turn_limit_can_only_reduce_global_limit`
  - `TestResolveUserRuntimeConfig.test_request_turn_limit_can_only_reduce_effective_limit`
  - `TestResolveUserRuntimeConfig.test_allow_mutations_false_restricts`
  - `TestResolveUserRuntimeConfig.test_request_true_cannot_reenable_disabled_mutations`
  - `TestResolveUserRuntimeConfig.test_all_overrides`
- Verify with:
  - `venv/bin/pytest tests/agents/test_llm_preferences.py -v`.
  - Targeted Python suites, then `venv/bin/pytest tests/agents -x`.
  - `npm run db:reset`, `npm run db:lint`, and `npm run db:migrations`.
  - `npm run build` to confirm the unchanged TypeScript/FastAPI contract.
  - `scripts/loadtest_concurrent.sh` with 12 users as a separate capacity check.
  - Supabase security and performance advisors after applying the migration.

Manual acceptance:

1. Set Jerry's preferences to `"llm": {"model": "deepseek-v4-pro", "reasoning_effort": "max"}` and confirm every completion in that invocation uses Pro/max.
2. Set Zachary's preferences to Flash/high and run both users concurrently; confirm no model, reasoning, turn-limit, or mutation-policy leakage.
3. Set `"execution": {"allow_mutations": false}` and confirm read tools remain available while mutating tools are excluded and blocked by the dispatcher.
4. Set a user turn limit below the process maximum and confirm the graph stops at the lower value; set one above the process maximum and confirm the global ceiling wins.
5. Resume an interrupted thread after changing its database preferences and confirm the saved snapshot remains in effect, subject to current global/request restrictions.

## Expected File Impact

Production changes:

- `agents/agent_api/app/user_context/preferences.py`: strict preference models and pure resolver, unless the resolver is placed in a focused sibling module.
- `agents/agent_api/app/graph/run_deps.py`: forced per-run model/reasoning fields.
- `agents/agent_api/app/graph/builder.py`: effective configuration resolution and accurate post-run metadata.
- `agents/agent_api/app/graph/nodes/orchestrator.py`: apply forced fields after router selection and record actual-model usage.
- `agents/agent_api/app/api/routes/invoke.py` and `resume.py`: preserve optional request overrides.
- `agents/agent_api/app/tools/selectors/static.py`, `keyword.py`, and `router.py`: exclude mutating schemas when resolved execution is read-only.
- `supabase/migrations/<generated>_extend_user_runtime_preferences_v1.sql`: database-side validation.
- `supabase/README.md`: administrative set/clear examples and paused-thread snapshot warning.

Test changes:

- `tests/agents/runtime_helpers.py`: `llm` and `execution` fixture inputs.
- `tests/agents/test_llm_preferences.py`: schema and resolver coverage.
- Existing API, router, run-dependency, usage-logging, runtime-resolver, and database-integration suites: precedence, isolation, telemetry, resume, and SQL checks.

## Assumptions

- Configuration remains administrator-managed; end users do not write `user_preferences` directly.
- Model and reasoning overrides are pinned; execution settings can only tighten global safety limits.
- Resume configuration is intentionally snapshot-frozen.
- Existing unrelated `.env.production.example`, `.env.sample`, and `reports/features/llm-provider-switching.md` worktree changes are user-owned and must remain untouched.
