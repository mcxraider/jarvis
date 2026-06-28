# Reliability 🟡

Resilience around the correct agent loop. The goal is that transient failures are handled automatically and the user always gets a useful, honest response.

> **Overall status (2026-06-24): PARTIAL.** LLM resilience (4.1) and Todoist retry (4.2) are done. Context-window management (4.3), batching (4.4), and error contract (4.5) are partial.


## 4.1b Duplicate and Clash Detection on Add ❌

**Status (2026-06-24):** Not started. No fuzzy matching or duplicate detection. No scheduling conflict checks.

**Original problem:** No pre-add validation exists. The agent blindly creates tasks without checking for duplicates or scheduling conflicts.

**Risk scenario:**
- User says "add gym at 6pm Thursday."
- A task "Gym" already exists for Thursday 6pm.
- Jarvis creates a duplicate without surfacing the conflict.
- Or: user says "add dinner at 7pm" but already has "team standup 6:45–7:15pm" — a time clash that goes unnoticed.

**Required behavior:**
- Before executing any `add_todoist_task` call, query existing tasks in the target time window and check for:
  1. **Duplicate detection** — fuzzy title match + same date/time within a tolerance window.
  2. **Schedule clash** — overlapping or adjacent events in the same time slot.
- If either is detected, surface the conflict to the user via HITL interrupt and require explicit reconfirmation before proceeding.
- Do not silently create the task.

**Trade-off:** Adds a read-before-write round-trip to every add operation; acceptable for a single-user assistant where correctness beats speed.

---

## 4.1c Audio Transcription Input Validation ❌

**Status (2026-06-24):** Not started. No pre-transcription length/size checks.

**Problem:** Audio messages are sent directly to the transcription service (Groq Whisper) without first-layer validation. Long recordings or oversized files could hit API limits, waste quota, or fail silently.

**Required behavior:**
- Check audio duration and file size before calling the transcription service.
- Reject or warn on messages exceeding provider limits (e.g., Whisper's 25MB / ~2hr cap).
- Surface a clear user-facing message ("Audio too long — please keep it under X minutes") rather than a raw API error.

---

## 4.3 Context-Window Management 🟡

**Status (2026-06-24):** Partial. Summarize node exists (`nodes/summarize.py`, threshold: `SUMMARIZE_THRESHOLD=20`) — compresses large tool results before next agent turn. **Gap:** Blanket `copy.deepcopy()` still used in all nodes. No rolling window or token-budget cap on message history.

**Original problem:** `copy.deepcopy(state["messages"])` runs on every node entry. `get_tasks` dumps full task JSON into `messages`. History grows each turn, so deepcopy cost grows quadratically and token cost balloons.

**Why it matters:** Latency and per-run cost scale badly on exactly the complex, multi-step tasks Jarvis needs to handle well.

**Fix:**
- Replace blanket `deepcopy` with targeted copies or append-only immutable message handling.
- Compress tool results before re-injection: store full payloads out-of-band; inject a compact projection (IDs + titles + due dates) into messages.
- Cap `messages` with a rolling window + summary once the thread exceeds a token budget.

**Trade-off:** Compression can drop a field the model later needs — keep a retrievable handle to the full payload.

---

## 4.4 Todoist Sync Batching and Pagination 🟡

**Status (2026-06-24):** Partial. Pagination mentioned in tool schemas but no batching implementation. Each tool call still makes individual REST requests.

**Original problem:** Each tool call is one Todoist REST request. Write-heavy operations (8 packing tasks) make 8 sequential network calls.

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

## 4.5 Error Handling Contract — User-Safe Failures 🟡

**Status (2026-06-24):** Partial. Error classification exists in `TodoistApiError`. Structured metadata preserved through `to_classifier_payload()`. TypeScript logger has PII redaction (`SENSITIVE_KEY_PATTERN`, `PRIVATE_ID_KEY_PATTERN` in `src/utils/logger.ts`). **Gap:** No comprehensive guarantee against raw stack traces leaking in edge cases. No deterministic partial-failure reporting.

**Original problem:** No consistent policy for what the user sees when things fail. Raw stack traces or silent partial failures are both possible.

**Goals:**
- Classify errors by source: Telegram, LLM, Todoist, validation, orchestration, network, timeout, configuration, unexpected runtime.
- Separate retryable from non-retryable errors.
- Preserve safe structured context for debugging: request IDs, chat IDs, tool names, operation names, status codes, retry attempts.
- Never expose API keys, private task content, full prompts, raw provider payloads, or stack traces in user-facing messages.
- Every user request ends with a clear Telegram response when possible.
- Partial failures are explicit: the user knows what succeeded, what failed, and what can be retried.

---

## 4.6 Final Node Checker — Post-Execution Hallucination Guard ❌

**Status (2026-06-24):** Not started. No dedicated verification node exists after execution completes.

**Original problem:** The orchestrator generates a final response based on its own memory of what it asked for, not on verified ground truth. It can claim "all 8 tasks added" when only 6 succeeded, or reference task fields that were never returned by Todoist.

**Why it matters:** Unlike 3.1 (which verifies *during* the loop before generating ANSWER), this is a terminal gate that catches hallucination in the final response itself — the last line of defense before the user sees the message.

**Required behavior:**
- A dedicated node runs after the agent produces its final response text but before it reaches the user.
- Cross-references claims in the response against actual `tool_results`: counts match, task names match, dates match, no invented fields.
- If the response asserts something not supported by execution facts, either correct it or flag it with a disclaimer.
- For bulk operations: verify claimed count vs. actual success count in `tool_results`.
- For mutations: verify the task IDs mentioned in the response actually appeared in tool results.

**Trade-off:** Adds one LLM call (or deterministic check) to every completed run. Could use a cheaper/faster model for this validation pass since it's a structured comparison, not creative generation.

---

## 4.7 Latency Enhancements ❌

**Status (2026-06-24):** Not started. Several opportunities identified but deferred — they would change architecture or behavior beyond simplification scope.

### Async Conversion for Blocking Paths

**Problem:** Some threads are currently blocked by slow synchronous processes (Todoist API calls, LLM inference waits). These synchronous bottlenecks prevent the event loop from servicing other requests while waiting on I/O.

**Fix:** Convert blocking call sites to proper async functions using `asyncio` / `aiohttp` so the event loop remains responsive during I/O waits. Priority targets: Todoist API calls in the executor, DeepSeek client interactions, and any sequential tool-execution paths that hold a thread while waiting on network responses.

### Findings to Skip (Architecture/Behavior Changes Beyond Simplification Scope)

The following opportunities were identified during review but deferred because they require significant refactoring, change runtime behavior, or touch architectural boundaries:

- **Extracting a shared `resilient_batch_execute` utility** — real duplication between `executor.py` and `client.py`, but the extraction is a significant refactor.
- **Extracting a shared DeepSeek client factory from `summarize.py`** — restructures node creation patterns.
- **Parallelizing sequential LLM calls in `summarize.py`** — architectural change to the hot path.
- **Reusing `ThreadPoolExecutor` across invocations** — lifecycle and thread-safety implications.
- **Unifying `MUTATING_TOOL_NAMES` with `metadata.py`** — larger registry consolidation effort.
- **Per-tool post-exec routing in `edges.py`** — future design direction, not a simplification.

---

## 4.8 Tool Results Verification and Batch Processing ❌

**Status (2026-06-25):** Not started. No tool-result integrity checks beyond the summarize node. No batch-processing threshold for large result sets.

**Original problem:** The summarize node (4.3) compresses large tool results to fit the context window, but compression alone does not guarantee reliability. When a user asks "show me all my tasks" and the result set is large (e.g., 50+ tasks), the summariser may silently drop items, miscount, or lose detail. The agent then confidently reports an incomplete or incorrect picture to the user.

**Why the summariser is not enough:**
- Summarisation optimises for token budget, not correctness. It can merge, abbreviate, or omit entries.
- The agent cannot reliably verify counts or completeness against a summary it didn't produce deterministically.
- Users asking "show me all" expect completeness — a summarised subset is a reliability failure even if the context window is managed.

**Required behavior — mandatory batch processing above a threshold:**
- Define a configurable threshold (e.g., `BATCH_RESULT_THRESHOLD=20`) for tool result item counts.
- When any tool returns a result set exceeding this threshold, the system MUST switch to batch-processing mode rather than passing the entire payload through the summariser:
  1. **Chunked presentation** — break results into pages/batches and present them incrementally, or summarise with an explicit "showing N of M" disclosure.
  2. **Deterministic counting** — maintain a verified count extracted directly from the tool result metadata (not re-counted by the LLM from message text).
  3. **Integrity check** — after processing, compare the number of items acknowledged/presented against the actual count returned by the tool. Flag discrepancies before responding to the user.
- This batch-processing rule applies universally — not just to `get_tasks`, but to ANY tool that can return a variable-length list (comments, projects, labels, search results, etc.).

**Implementation sketch:**
- A pre-injection gate inspects `tool_results` length before they enter the message stream.
- If count > threshold: route through a batch-processing path that paginates, counts deterministically, and injects a structured summary with explicit totals.
- If count ≤ threshold: pass through normally (summariser is acceptable for small sets).
- The final response must always include the verified total when reporting on a collection ("Here are your 47 tasks" — where 47 is extracted from the tool result, not hallucinated).

**Trade-off:** Batch processing adds complexity and may require multiple message turns for very large sets. But the alternative — confidently reporting incomplete data — is a worse failure mode for a personal assistant.
