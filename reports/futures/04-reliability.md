# Reliability

Resilience around the correct agent loop. The goal is that transient failures are handled automatically and the user always gets a useful, honest response.

---

## 4.1 LLM Call Resilience

**Status:** `DeepSeekAgentClient.create_message` has no try/except, no retry, and no timeout. A transient 429/500 or dropped connection throws straight up and kills the entire run.

**Fix:**
- Wrap the call with bounded retry + exponential backoff + jitter (e.g. `tenacity`).
- Set an explicit request timeout on the OpenAI client.
- Retry only on 429 / 5xx / timeout — never on 4xx schema errors.
- On final failure, write a structured error into `JarvisState["error"]` so the graph ends gracefully instead of raising to the caller.

**Trade-off:** Retries add latency and cost on genuinely broken requests; cap attempts and total elapsed time.

---

## 4.2 Todoist API Retry and Error Taxonomy

**Status:** Done in branch `todoist-api-retry-taxonomy`.

**Implemented:**
- `TodoistApiClient._request` now classifies errors as rate-limit, transient, auth, validation, not-found, or deprecated.
- Transient and rate-limit failures retry with bounded backoff and `Retry-After` support.
- Auth, validation, missing config, not-found, and deprecated endpoint failures are never retried.
- Todoist failures now surface safe structured `classified_error` metadata through tool results instead of raw provider bodies.
- Retry tuning is environment-backed with defaults for max attempts, total retry budget, base delay, and max delay.

**Validation:**
- Added Python tests for retry success, retry exhaustion, `Retry-After`, non-retryable classifications, missing API key, `URLError`, and dispatcher/graph propagation.
- Verified with `/Users/Jerry_YANG_from.TP/Desktop/jarvis-mcp/venv/bin/python -m unittest tests.agents.test_jarvis`.
- Verified with `/Users/Jerry_YANG_from.TP/Desktop/jarvis-mcp/venv/bin/python -m compileall agents/agent_api/app tests/agents/test_jarvis.py`.

---

## 4.3 Context-Window Management

**Status:** `copy.deepcopy(state["messages"])` runs on every node entry. `get_tasks` dumps full task JSON into `messages`. History grows each turn, so deepcopy cost grows quadratically and token cost balloons.

**Why it matters:** Latency and per-run cost scale badly on exactly the complex, multi-step tasks Jarvis needs to handle well.

**Fix:**
- Replace blanket `deepcopy` with targeted copies or append-only immutable message handling.
- Compress tool results before re-injection: store full payloads out-of-band; inject a compact projection (IDs + titles + due dates) into messages.
- Cap `messages` with a rolling window + summary once the thread exceeds a token budget.

**Trade-off:** Compression can drop a field the model later needs — keep a retrievable handle to the full payload.

---

## 4.4 Todoist Sync Batching and Pagination

**Status:** Each tool call is one Todoist REST request. Write-heavy operations (8 packing tasks) make 8 sequential network calls.

**Rate limits to design around:**

| Area | Limit |
|---|---|
| Partial sync requests | 1,000 / 15 min / user |
| Full sync requests | 100 / 15 min / user |
| Commands per sync request | 100 |
| POST body size | 1 MiB |
| Standard timeout | 15 seconds |

**Batching behavior:**
- Use full sync only for initial state hydration; use incremental/partial syncs after that.
- Send compatible independent mutations in one batched request when safe.
- Split oversized plans into chunks respecting command and body-size limits.
- Preserve per-item success, failure, and skipped status even when the wire request is batched.
- Broad or risky batches still require confirmation before side effects.

**Pagination behavior:**
- Tool wrappers should auto-paginate by default when providers return pagination markers.
- If pagination cannot be completed, the return-to-user node must explicitly say the answer may be partial.

---

## 4.5 Error Handling Contract — User-Safe Failures

**Status:** No consistent policy for what the user sees when things fail. Raw stack traces or silent partial failures are both possible.

**Goals:**
- Classify errors by source: Telegram, LLM, Todoist, validation, orchestration, network, timeout, configuration, unexpected runtime.
- Separate retryable from non-retryable errors.
- Preserve safe structured context for debugging: request IDs, chat IDs, tool names, operation names, status codes, retry attempts.
- Never expose API keys, private task content, full prompts, raw provider payloads, or stack traces in user-facing messages.
- Every user request ends with a clear Telegram response when possible.
- Partial failures are explicit: the user knows what succeeded, what failed, and what can be retried.
