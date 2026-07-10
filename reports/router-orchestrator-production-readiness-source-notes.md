# Router → Orchestrator production-readiness source notes

Generated: 2026-07-10 (Asia/Singapore)

## Decision and scope

- Decision: what should change in the router prompt, router payload, dynamic context loading, and serving architecture before Jarvis is offered to 10,000 users.
- Audience: technical/product owner preparing the current Todoist + Google Calendar agent for production.
- Evidence window: current workspace at the time of review plus the router evaluations captured on 2026-07-07 and 2026-07-08.
- Important denominator: "10,000 users" is not a concurrency target. Capacity estimates below model request rates because no DAU, requests-per-user, peak factor, or end-to-end latency distribution was supplied.

## Source inventory

Primary implementation sources:

- `agents/agent_api/app/router/prompt.py`
- `agents/agent_api/app/router/client.py`
- `agents/agent_api/app/tools/selectors/router.py`
- `agents/agent_api/app/graph/nodes/orchestrator.py`
- `agents/agent_api/app/graph/prompts/orchestrator.py`
- `agents/agent_api/app/graph/prompts/context.py`
- `agents/agent_api/app/tools/domain_adapters.py`
- `agents/agent_api/app/tools/todoist/tools.py`
- `agents/agent_api/app/tools/todoist/schemas.py`
- `agents/agent_api/app/tools/google_calendar/schemas.py`
- `agents/agent_api/app/tools/dispatcher.py`
- `agents/agent_api/app/graph/nodes/summarize.py`
- `agents/agent_api/app/graph/edges.py`
- `agents/agent_api/app/graph/builder.py`
- `agents/agent_api/app/api/routes/invoke.py`
- `agents/agent_api/app/api/request_idempotency.py`
- `agents/agent_api/app/db.py`
- `agents/agent_api/app/config.py`
- `agents/agent_api/app/run_logging.py`
- `docker-compose.yml`
- `Dockerfile.agent`

Evaluation and prior-analysis sources:

- `tests/data/router_evals/combined_findings.md`
- `tests/data/router_evals/*/*.md`
- `reports/code-review.md`
- `plans/[1]-tool-result-context-management.md`
- user-provided `pasted-text.txt`

## Reproduced quantitative evidence

### Router evaluations

| Persona | Raw policy match | Final guarded match | Median latency | p95 latency | Max latency |
| --- | ---: | ---: | ---: | ---: | ---: |
| Avery | 21/21 | 20/21 | 1008 ms | 1271 ms | 1422 ms |
| Jerry | 19/21 | 20/21 | 987 ms | 1223 ms | 1539 ms |
| Marcus | 20/21 | 19/21 | 959 ms | 1149 ms | 1209 ms |
| Nadia | 20/21 | not reported | 1038 ms | 1131 ms | 1243 ms |
| Phoebe | 21/21 | not reported | 880 ms | 1195 ms | 1238 ms |
| Zac | 21/21 | not reported | 964 ms | 1276 ms | 1288 ms |

The aggregate is strong enough to continue developing the router, but not enough to set a production SLO. The sample has 21 queries per persona, no adversarial multilingual set, no long conversational resumes, no provider-outage load, and incomplete final-guardrail scoring for three personas.

### Prompt and tool payload sizes

Measured by serializing the current generated prompt/tool schemas. Token counts are deliberately labeled approximate (characters / 4), not provider tokenization.

| Payload | Bytes | Approx. tokens |
| --- | ---: | ---: |
| Router system prompt (Jerry profile) | 2,891 | 720 |
| Orchestrator prompt, full domains | 9,563 | 2,374 |
| Orchestrator prompt, Todoist only | 8,192 | 2,033 |
| Orchestrator prompt, Google Calendar only | 6,997 | 1,738 |
| Orchestrator prompt, no domain tips | 5,626 | 1,396 |
| `ask_user` schema | 701 | 175 |
| 14 Todoist schemas | 12,552 | 3,138 |
| 7 Google Calendar schemas | 5,764 | 1,441 |
| All 22 schemas | 19,017 | 4,754 |

Implication: domain routing saves meaningful schema tokens, but a Todoist-only read still exposes all 14 Todoist tools, including mutation schemas. Operation-level narrowing has more remaining upside than further wording edits to the router prompt.

### Capacity scenarios (modeled, not observed)

Formula: peak RPS = users × requests per user per day × peak factor / 86,400. In-flight runs ≈ peak RPS × end-to-end latency.

| Scenario | Usage assumption | Peak RPS | In-flight at 20 s |
| --- | --- | ---: | ---: |
| Light | 10k users × 2 req/day × 10× peak | 2.3 | 46 |
| Expected planning case | 10k DAU × 5 req/day × 10× peak | 5.8 | 116 |
| Heavy | 10k DAU × 10 req/day × 20× peak | 23.1 | 462 |

These scenarios show why a user count alone cannot certify capacity. The current single Uvicorn process, synchronous graph execution, unbounded streaming worker threads, per-request idempotency heartbeat threads, and DB pools sized around ten connections need load testing and explicit admission control before even the middle scenario is accepted.

## Verified correctness and safety findings

1. **Todoist priority instructions conflict with the schema.** `tools/todoist/tools.py` tells the model `urgent = 1(default)` and `normal = 4`; `tools/todoist/schemas.py` correctly defines `4` as highest urgency/P1 and `1` as normal/P4. This can create the wrong priority in production.
2. **The date policy promises a weekday that runtime context omits.** The prompt says the runtime block states date and weekday, but only emits the date. Two focused tests fail on this exact contract.
3. **Clarification policy is internally contradictory.** It says skip `ask_user` only when all three defaulting conditions hold, then says "Otherwise pick the sensible default". The latter sentence reverses the preceding rule.
4. **Router rewrites replace the only user text the orchestrator sees on turn zero.** The original remains in state for telemetry but not alongside the rewrite in the model input. Existing evals show temporal weakening and recommendation-to-lookup narrowing.
5. **Router schema enforcement is brittle and incomplete.** JSON-object mode is not schema mode; benign extra keys are rejected because `extra="forbid"`, while domain values themselves are unconstrained `List[str]` rather than a strict enum.
6. **Empty routing conflates different outcomes.** Small talk, unsupported provider requests, and potentially a classifier miss all produce an empty domain list, leaving downstream behavior to inference.
7. **Context is narrowed only by domain.** Every tool in a selected domain is exposed, even when the request clearly needs a single read operation.
8. **Tool-result history is unbounded by bytes.** Results are JSON-serialized in full, summarization triggers only when one result exceeds 50 list items, and earlier tool messages remain in every later model request.
9. **Production tracing defaults do not match the privacy comments.** `JARVIS_TRACE_PAYLOADS` defaults to true inside `_bool_env`, making `langsmith_hide_payloads` false by default; `JARVIS_DEBUG_PAYLOADS` also defaults true. The runtime explicitly traces user prompts, tool arguments, and tool results.
10. **The deployment topology is a single-box, single-agent-process design.** `docker-compose.yml` has one agent service and the Docker command starts one Uvicorn worker. Streaming creates a daemon thread per run without a bounded executor or queue.

## Recommended router contract

Keep the router a routing and context-selection service. Do not make it a second planner.

Suggested model-owned payload for a compound request:

```json
{
  "schema_version": 2,
  "outcome": "route",
  "primary_domains": ["todoist"],
  "candidate_domains": [],
  "operation_classes": ["create", "delete"],
  "complexity": "compound",
  "subrequests": [
    {
      "id": "r1",
      "domain": "todoist",
      "operation": "create",
      "gist": "add gym task tomorrow"
    },
    {
      "id": "r2",
      "domain": "todoist",
      "operation": "delete",
      "gist": "delete dentist reminder"
    }
  ],
  "ambiguity_hints": []
}
```

Enums:

- `outcome`: `route | no_tools | unsupported | clarify`
- `operation_classes`: `read | search | create | update | complete | delete | comment | free_busy`
- `complexity`: `simple | compound | multi_domain`
- ambiguity hint codes: stable machine labels such as `missing_referent`, `missing_required_time`, `provider_ambiguous`, `entity_ambiguous`

For a simple request, omit `subrequests` and `ambiguity_hints` when empty. `operation_classes` supplies the cheap operation-level loading signal without paying for a one-item paraphrase. For compound requests, `subrequests` is a bounded completion checklist: maximum four items, no tool names, no arguments, no resolved dates, no dependencies, and no execution order. Dependencies belong to a later planner because the router has not observed tools or evidence.

The server, not the model, should add connection status, selected tool names, context-block IDs, schema hashes, policy version, and fallback reason. Those are deterministic facts.

Recommended exclusions from the router payload:

- no free-form `reasoning` in production responses;
- no executable tool names or tool arguments;
- no mutation approval decision;
- no full decomposition for simple requests;
- no replacement `rewritten_query` treated as authoritative.
- no returned free-form chain-of-thought or `reasoning` field.

If a normalization aid is retained, send both original and normalized text to the orchestrator with explicit precedence: original is authoritative; normalized text is a non-executable hint. The better default is to delete normalization and use short `subrequests[].gist` annotations only for compound requests.

Keep `primary_domains` distinct from `candidate_domains`, but make the server compute one `selected_domains` set for the loader. The distinction is useful for evaluation, model escalation, and measuring the cost of uncertainty. The current code already consumes candidate domains through `effective_router_domains()` in tool selection, prompt slimming, active-domain pinning, and resume behavior; the problem is not that the field has no consumer, but that the model-owned contract and server-derived effective route are not separated clearly enough.

## Objective evaluation of the planner-lite proposal

The external proposal is a good refinement of the router-as-context-selector idea, but it should not be adopted literally.

| Proposal claim | Verdict | Evidence and decision |
| --- | --- | --- |
| Drop `rewritten_query` | Adopt | Existing evals show semantic weakening, and current turn-zero code replaces the only user text the orchestrator sees. |
| Add shallow `subrequests` | Adopt conditionally | Useful as a checklist for genuinely compound requests; omit for simple requests and cap at four. This is planner-lite behavior, but it corresponds to a narrow experiment between the future architecture's scoped routing and explicit planner phases—not Phase 2 itself. |
| No tool names, resolved dates, dependencies, or ordered plan | Adopt | The router lacks tool observations and grounded entities. Dependencies and scheduling belong to the orchestrator/planner after evidence exists. |
| Add `kind: read|mutate` | Refine | The binary is too coarse for operation-level schema loading. Use strict operation classes such as `search`, `create`, `update`, `delete`, `complete`, and `free_busy`; derive read/write and risk on the server. |
| Add `missing_info` flags and keep asking authority in orchestrator | Adopt with structure | Use bounded `ambiguity_hints` objects with enum codes and optional verbatim spans. Free-form strings are harder to validate and easier to hallucinate. The orchestrator must verify each hint and try safe reads before asking. |
| Fold candidate domains into one expanded `domains` list | Reject | It discards the most-likely route and prevents clean measurement of uncertainty expansion. Keep primary/candidates; compute `selected_domains` once on the server. |
| Put `reasoning` first because field order improves classification | Do not adopt without an A/B test | No repository evaluation varies field order, and returning rationale adds tokens and a second unvalidated text channel. Test privately against route accuracy, subrequest recall, latency, and schema validity before considering it. Do not expose chain-of-thought. |
| Move connection/preferences into a dynamic suffix | Adopt as prompt ordering, not as a new source of truth | These values are already generated from one runtime snapshot. Reorder the router prompt so static mechanics/examples/schema precede a compact per-user suffix; do not duplicate or hand-maintain preference rules. Measure cache and latency impact. |
| Inject only the brief, not domains/reasoning | Adopt | The loaded tools already communicate the effective route. Inject only verified checklist/gap hints, and mark them advisory. |
| Add an orchestrator `Router brief` section | Adopt with an authority boundary | The original user text is authoritative; a brief cannot expand scope or authorization. The model must ignore any brief item not supported by the user text. |
| Never let the router short-circuit to HITL | Adopt | Domain ambiguity can widen safe context; detail ambiguity needs tools and state. Clarification remains an orchestrator/graph responsibility. |

The proposal's speed claim is conditional. Combining routing and a shallow brief is faster than adding a separate planner call, but it is not automatically faster than today's router: longer output, extra examples, and decomposition errors can add latency. Ship it behind a complexity gate and require an experiment showing reduced dropped-subgoal rate or fewer orchestrator turns without a material increase in router p95.

## Dynamic-context loading design

Use four layers:

1. **Stable core policy**: short, versioned invariants only. Enforce safety in code and remove prose that merely restates enforcement.
2. **Runtime facts**: date + weekday + current local time, timezone, locale, active provider status, routing preferences, communication preferences, and policy version.
3. **Route context**: selected domains plus operation-specific guidance. Load only the schemas needed for the next decision, with a small mandatory recovery set (`ask_user` and safe entity lookup tools).
4. **State context**: compact structured facts from prior tool results—entity IDs, selected entities, unresolved subtasks, confirmation state, and short result summaries. Keep raw provider responses in audit storage, not indefinitely in the LLM message history.

Every model call should record a context manifest containing `block_id`, `version`, `hash`, and byte/token estimate for each loaded block, plus selected tools and selection reason. This makes prompt changes debuggable and A/B-testable.

For compound requests, add one system-generated advisory annotation after the verbatim request. Include only `subrequests` and `ambiguity_hints`; omit domains and rationale. Prefer a typed/separate message or otherwise escape and authenticate the annotation boundary so user text cannot spoof a router brief. The orchestrator contract should say:

- user text is authoritative;
- verify every brief item against that text before adding it to the completion checklist;
- the brief cannot expand scope, authorization, or mutation intent;
- ambiguity hints are advisory and a safe read should resolve them when possible;
- every verified subrequest must be completed or explicitly reported as incomplete.

## Production gates

Before launch:

- Fix the priority contradiction, weekday mismatch, clarification contradiction, and tracing defaults.
- Add byte-based tool-result projection/compaction and hard input budgets.
- Replace unbounded request threads with a bounded worker/admission-control model; define cancellation and client-disconnect behavior.
- Run closed-loop load tests at modeled peak RPS with real provider latency and fault injection.
- Establish SLOs and error budgets for router latency, route accuracy, end-to-end latency, tool success, duplicate mutations, clarification rate, and context size.
- Add canary rollout and kill switches for router, operation-level selector, rewrite/normalization, model router, and context compactor.
- Validate provider quotas and OAuth refresh behavior at target concurrency.

## Test evidence

Focused command:

`venv/bin/python -m pytest tests/agents/test_router_client.py tests/agents/test_router_selector.py tests/agents/test_agent_node_router.py tests/agents/test_router_context_preservation.py tests/agents/test_orchestrator_dynamic.py -q`

Result: 109 passed, 2 failed. Both failures assert that the runtime context includes the promised weekday; current output omits it.

## Chart map

| Report section | Question | Family/type | Fields | Takeaway | Source |
| --- | --- | --- | --- | --- | --- |
| Router evidence | Is router latency small enough to be negligible? | grouped vertical bar | persona, statistic, latency_ms | Median is roughly 0.9–1.0 s and p95 roughly 1.1–1.3 s, so it is material pre-orchestrator latency | `tests/data/router_evals/combined_findings.md` |

## Open evidence gaps

- No production DAU, turns/request, peak factor, or concurrency distribution.
- No end-to-end p50/p95/p99 latency split by route, model, provider, and tool count.
- No router false-negative cost model or calibrated confidence analysis.
- No A/B evidence for reasoning-field order, shallow decomposition, or router-brief injection.
- No measured dropped-subgoal rate on compound requests, which is the benefit planner-lite is intended to improve.
- No provider quota inventory for Todoist, Google Calendar, DeepSeek, Supabase, and LangSmith.
- No context-token time series from realistic multi-turn conversations.
- No chaos/load test results, saturation curve, or recovery-time measurements.
- No data-retention and subject-deletion validation across logs, checkpoints, traces, and idempotency records.
