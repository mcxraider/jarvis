# Observability & Testing

Visibility into system behavior and a testing strategy. The system should be debuggable before scaling, not after.

---

## 6.1 Metrics and Cost Accounting

**Status:** `TracePrinter` prints human-readable lines; LangSmith captures traces. No metrics, no token/cost accounting per run, logs are not machine-parseable.

**Fix:**
- Replace `print` with a structured JSON logger; include `thread_id` as a correlation key in every log line.
- Read `response.usage` from each DeepSeek response and accumulate into `JarvisState`.
- Export counters/histograms: tool success %, turns per run, p50/p95 latency, cost per run, tokens in/out.
- Redact task content (potential PII) in logs and traces.

**Key metrics to track:**

| Metric | Why |
|---|---|
| Graph node duration | Find slow nodes |
| Tool selection confidence | Alert on poor routing |
| Tool call success/failure/retry rate | Track reliability |
| Clarification rate | Signal prompt ambiguity |
| HITL waiting time | UX signal |
| Safety block count | Security signal |
| Token cost per run | Budget control |
| Pagination fallback count | Coverage signal |
| Active-run lock contention | Concurrency signal |

---

## 6.2 Eval Harness and Regression Tests

**Status:** No tests or eval. `USER_PROMPTS` is a manual smoke list with no assertions.

**Why it matters:** Every prompt or model tweak is an uncontrolled change. Regressions in tool-call sequences are invisible.

**Three-layer testing strategy:**

### Unit tests (no live dependencies)
- Dispatcher, mutation guard, validation, reference resolution.
- Use an injected `FakeTodoistClient` (the seam already exists via the client injection pattern).
- Assert: tool name called, args passed, errors returned, HITL triggered.
- Cover: planner output counts, tool selection narrowing, tool health filtering, idempotency deduplication, safety-monitor flagging, return-to-user field selection.

### Scenario evals (mock LLM + mock Todoist)
- Scenario set with expected tool-call sequences per prompt.
- Assert tool name/arg sequences (allow-set, not exact match — LLM nondeterminism).
- LLM-as-judge on final answers for quality/grounding.
- Golden-trace diffs for representative flows: simple add, bulk add, follow-up mutation, reference resolution, HITL clarification, tool-unavailable, safety block.

### Live gated tests (real APIs, explicit env flag)
- Optional: create 2+ dated tasks from one message.
- Optional: reference resolution after listing tasks.
- Optional: daily brief against real Todoist data.
- Never run in CI by default; gated by `LIVE_TESTS_ENABLED=true`.

---

## 6.3 Integration Tests (TypeScript ↔ Python)

- TypeScript message processor calls the Python bridge and handles all response states: `completed`, `clarification`, `blocked`, `tool_unavailable`, `failed`.
- Mid-run Telegram messages are ignored or routed to status without starting a second graph.
- Telegram force-reply clarification messages resume the existing paused graph.
- Mixed success/failure produces a deterministic report.
- Simulated Todoist 429, 503, 410, and malformed errors all route correctly.
- Retry after a post-creation crash reuses the stored external ID (idempotency).
- Safety monitor can stop an in-flight graph before tool execution.

---

## 6.4 Rollout Sequence

Recommended order for enabling features in production:

1. Python LangGraph service + TypeScript bridge behind a feature flag.
2. Tool selection layer + registry metadata.
3. Tool error classifier + recovery router + registry health checks.
4. Parallel safety monitor + cancellation.
5. Idempotency store for mutating operations.
6. Error handling, retry classification, user-safe failures.
7. Follow-up context + reference resolution.
8. Return-to-user node with verified-facts-only output.
9. Telegram conversation gating + HITL routing.
10. Stateful progress messages (separate feature flag).
11. Observability across both runtimes.
12. Scheduled jobs for daily briefs (separate feature flag).
13. Todoist sync batching.
14. Audio transcription routed through the same orchestrator.
