# MCP Migration Design Document

**Protocol Version:** MCP 2025-06-18 spec
**Status:** Reviewed 2026-07-25 — see "Review Verdict & Recommendation" at the end. Spec corrections applied inline.

## Summary

Replace the custom `ToolSpec`/`ToolRegistry`/`DomainAdapter` infrastructure with Model Context Protocol (MCP) servers — one per domain. Each domain becomes a standalone MCP server process exposing tools over stdio (local) or Streamable HTTP (remote). The LangGraph agent becomes an MCP client that discovers tools at runtime.

**Goal:** Adding a new domain to Jarvis should require only writing one MCP server (schema + handler) and registering its connection — no changes to the graph, dispatcher, selector, or prompt assembly.

---

## Current Architecture (What's Being Replaced)

```
DomainAdapter (frozen dataclass)
├── build_client(credential, tracer)  → domain-specific API client
├── get_tool_specs(client)            → list[ToolSpec]  (name, schema, handler, mutating flag)
├── prompt_fragment                   → injected into system prompt when domain active
└── credential_validator              → optional health probe

ToolRegistry  → aggregates ToolSpecs from all active domains
ToolDispatcher → mutation guard, idempotency, error classification, batch execution
ToolSelector  → filters schemas per-turn (static / keyword / LLM router)
```

**Coupling points that make new domains expensive:**
1. Must implement `ToolSpec` + handler following internal conventions
2. Must register in `DOMAIN_ADAPTERS` dict
3. Must wire credential resolution in `registry_factory.py`
4. Prompt fragments manually composed in orchestrator
5. Router/keyword selector tables need updating
6. Risk classification in `graph/risk.py` needs new tool names

---

## Target Architecture

```
┌────────────────────────────────────────────────────────┐
│  LangGraph Agent (MCP Client)                          │
│                                                        │
│  ┌──────────────┐  ┌──────────────┐  ┌─────────────┐  │
│  │ MCP Session  │  │ MCP Session  │  │ MCP Session  │  │
│  │  (Todoist)   │  │  (GCal)      │  │  (Gmail)     │  │
│  └──────┬───────┘  └──────┬───────┘  └──────┬──────┘  │
└─────────┼──────────────────┼─────────────────┼─────────┘
          │ stdio            │ stdio            │ stdio
┌─────────▼───────┐  ┌──────▼───────┐  ┌──────▼──────┐
│ todoist-mcp     │  │ gcal-mcp     │  │ gmail-mcp   │
│ (MCP Server)    │  │ (MCP Server) │  │ (MCP Server) │
└─────────────────┘  └──────────────┘  └─────────────┘
```

Each MCP server:
- Exposes tools via the MCP tool protocol (JSON-RPC over stdio or Streamable HTTP)
- Owns its own credential handling (passed via a private post-init `set_credentials` message — see Design Decision 2; MCP has no standard stdio credential channel)
- Declares tool metadata: `name` (identifier), `title` (human-readable display), description, `inputSchema`, `outputSchema` (typed results), and annotations (`readOnlyHint`/`destructiveHint`/`idempotentHint`/`openWorldHint`)
- Exposes **prompts** (replaces `prompt_fragment`) and **resources** (replaces grounding notes)
- Validates all inputs at its own boundary (existing Pydantic schemas move into the server)

---

## MCP Primitives Usage

The migration uses all three MCP server primitives — not just tools.

| Primitive | Current Jarvis Feature | MCP Mapping |
|-----------|----------------------|-------------|
| **Tools** | `ToolSpec` handlers | `tools/list` + `tools/call` — the core migration |
| **Resources** | `GROUNDING_NOTE` per domain | `resources/list` with `audience: ["assistant"]` — *our client code* injects into context (MCP does not auto-inject; this is client-side behavior we own) |
| **Resources** | Entity grounding (project names, calendar IDs) | Subscribable resources — client pre-fetches without a tool call. Never credentials/secrets. |
| **Prompts** | `PROMPT_FRAGMENT` per domain | `prompts/get` with domain name — prepended to system prompt. Note: spec defines prompts as *user-controlled* (slash-command-like); using them as system-prompt fragments is a private convention between our client and servers. |

Phase 2 defines which domain context moves to resources vs. prompts vs. stays as tool descriptions.

---

## Migration Plan

### Phase 1: MCP Client Layer in LangGraph

**What:** Add an MCP client adapter that presents MCP-discovered tools as the existing `ToolSpec` interface, so the rest of the graph doesn't change yet.

**Files to create:**
- `agents/agent_api/app/tools/mcp_adapter.py` — thin shim converting MCP tools → `ToolSpec`. Evaluate `langchain-mcp-adapters` (official LangChain package) for the session/transport plumbing before hand-rolling a client layer; we likely only need the `ToolSpec` conversion on top of it.

**Result-shape invariant (safety-critical):** `graph/extractors.py`, `entity_index.py`, `summarize.py`, and `formatting/tool_tree.py` all parse raw tool results. The adapter must unwrap MCP `content`/`structuredContent` envelopes back to the exact shapes these consumers expect, or entity validation (the hallucination guard) silently degrades. Add a contract test asserting unwrapped MCP results are byte-identical to direct-handler results for the same fixture inputs.

**Key decisions:**
- Transport: **stdio** for co-located servers (simplest, no network). Keeps deployment identical — one container, child processes.
- Session lifecycle: **one MCP session per (user, domain, conversation)**. Spawn on first tool call for that domain, keep alive for the conversation duration (matches `RuntimeContextSnapshot` lifecycle). Reap stale sessions after conversation timeout.
- The adapter reads MCP tool annotations to populate `mutating` flag (`readOnlyHint: false` → mutating). Note: annotations are "hints" per spec — trusted here because we own all servers.
- The adapter reads `outputSchema` from tools to get typed results (improvement over current arbitrary dict returns).
- Credential injection: **correction — `initializationOptions` is an LSP concept and does not exist in MCP.** The MCP `initialize` request carries only `protocolVersion`, `capabilities`, and `clientInfo`. Since we own both sides, use a private extension: the client sends a `set_credentials` tool call (or custom notification) immediately after init, before any real tool call. This is a documented private convention, not spec behavior. Env vars remain rejected (`/proc/PID/environ` exposure); CLI args are worse (`/proc/PID/cmdline` is world-readable). If we later move to Streamable HTTP, switch to the standard `Authorization` header.

**Rollback mechanism:**
```python
DOMAIN_TRANSPORT = {
    "todoist": "mcp",       # use MCP server
    "google_calendar": "direct",  # use legacy DomainAdapter
}
```
Migrate one domain at a time, roll back instantly by flipping config.

**Latency benchmark:** Before merging, measure actual Python MCP server cold-start and stdio round-trip. Budget: **MCP overhead must add <100ms to current tool execution path.** If exceeded, implement warm pool before proceeding.

**Result:** The graph sees the same `ToolRegistry` interface. Old `DomainAdapter` and new `McpDomainAdapter` coexist.

### Phase 2: Convert Todoist to MCP Server

**What:** Extract `tools/todoist/` into a standalone MCP server package.

**Structure:**
```
mcp-servers/
└── todoist/
    ├── pyproject.toml
    ├── server.py             # MCP server entry (FastMCP)
    ├── client.py             # TodoistApiClient (moved from tools/todoist/client.py)
    ├── schemas.py            # input schemas (reuse existing Pydantic validation)
    └── handlers.py           # tool handlers
```

**Server implementation (Python + `mcp` SDK):**
```python
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("todoist")

@mcp.tool(annotations={"readOnlyHint": True})
async def get_tasks(project_id: str | None = None, filter: str | None = None) -> list[dict]:
    """Get tasks from Todoist."""
    ...

@mcp.tool(annotations={"destructiveHint": True})
async def delete_task(task_id: str) -> dict:
    """Delete a task."""
    ...
```

**Naming convention:** `name` = snake_case identifier (matches current tool names). `title` = human-readable ("Get Tasks", "Delete Task").

**Credential passing:** Orchestrator sends the private `set_credentials` message right after MCP init (see Design Decision 2). Server refuses all other tool calls until credentials are set.

### Phase 3: Convert Google Calendar to MCP Server

Same pattern. Token rotation: **correction — do not route tokens through MCP resources.** Resources exist to expose context to the LLM/user; a subscribable resource containing an OAuth token puts a secret on the same surface the client injects into model context. Instead, the server owns the refresh flow entirely: it already holds the credential, refreshes when needed, and persists the rotated token to the credential store itself (it gets the store handle in `set_credentials`). No protocol traffic carries secrets after init.

### Phase 4: Remove Legacy Infrastructure

Once all domains are MCP servers:
- Delete `tools/domain_adapters.py`, `tools/base.py` (ToolSpec/ToolRegistry), `registry_factory.py`
- `ToolDispatcher` simplifies: mutation guard + idempotency remain, but execution delegates to MCP `call_tool` instead of direct handler invocation
- Tool selection can use MCP's native tool list filtering or remain as-is (router just filters schema names)

### Phase 5: New Domains as Pure MCP Servers

Adding Gmail, Notion, GitHub, etc. becomes:
1. Write the MCP server (standalone, testable in isolation)
2. Register it in a domain config (server command + credential mapping)
3. Done — no graph/dispatcher/prompt changes

---

## Key Design Decisions

### 1. Session Lifecycle & Multi-Tenancy

One MCP session per **(user, domain, conversation)**. This matches `RuntimeContextSnapshot` lifecycle.

- **Spawn:** on first tool call for that domain in the conversation
- **Keep alive:** for the conversation duration (3-5 tool calls per turn amortized)
- **Reap:** after conversation timeout or graceful shutdown signal
- **Credential rotation mid-session:** Server owns refresh and persists rotated tokens directly (see Phase 3). Never expose tokens as MCP resources.
- **Graceful shutdown:** In-flight `call_tool` gets a JSON-RPC cancel. Server has 5s to clean up before SIGKILL.
- **Crash recovery:** If the subprocess dies mid-conversation, the adapter respawns it and replays init + `set_credentials` before the next call. A held call approved via HITL whose session died must be re-dispatched through the fresh session — the `canonicalize.py` hash binds the call payload, not the session, so this is safe, but the executor needs an explicit respawn-and-retry path (one attempt, then surface a classified transient error).

### 2. Credential Isolation

Credentials via a private post-init `set_credentials` message (MCP has no standard stdio credential channel — see Phase 1 correction):
- Avoids `/proc/PID/environ` exposure (env vars) and `/proc/PID/cmdline` exposure (args)
- Supports credential rotation without process restart (re-send `set_credentials`)
- Clean separation: orchestrator owns credential resolution, server owns API calls
- Explicitly a private extension between our client and our servers; incompatible with third-party MCP clients by design

### 3. Risk Classification via Annotations

Note: `graph/risk.py` already derives `RISKY_TOOLS` from the declarative `tools/metadata.py` registry — it is not hardcoded per tool name today. The migration moves that metadata from one declarative table to MCP annotations; the benefit is co-location with the server, not deleting hardcoded lists.

MCP tool annotations replace the `metadata.py` risk table:
- `destructiveHint: true` → risky, requires HITL confirmation
- `readOnlyHint: true` → safe, no confirmation needed
- `idempotentHint: true` → safe for retry

**Trust model:** Since we own all servers, annotations are trusted. If adopting third-party MCP servers in future, treat annotations as untrusted and maintain an override list.

### 4. Idempotency

Stays at the dispatcher level (MCP client side). The MCP server is a stateless executor; deduplication stays in the orchestrator where it has thread/turn context.

### 5. Error Contract

MCP servers return typed errors via `structuredContent` with `isError: true` (`outputSchema`/`structuredContent` are in the 2025-06-18 spec and supported by the Python SDK — no need to stuff JSON into a text field):

```json
{
  "isError": true,
  "structuredContent": {
    "error_class": "rate_limited",
    "retryable": true,
    "retry_after_seconds": 30,
    "message": "Todoist API rate limit exceeded"
  }
}
```

The client adapter maps `structuredContent` to `ClassifiedApiError` (retryable / ambiguous / permanent). Protocol-level JSON-RPC errors (unknown tool, invalid args) are treated as permanent/non-retryable.

### 6. Tool Selection / Router

Unchanged. The router still selects which tool schemas to present to the LLM. It reads them from MCP `tools/list` instead of the in-process registry. Tool list cached per session (won't change mid-request).

---

## Observability & Debugging

| Concern | Solution |
|---------|----------|
| **Correlation IDs** | Pass `trace_id` in MCP tool call arguments (or request metadata). Server logs include it. |
| **LangSmith tracing** | Each MCP server runs its own LangSmith/OpenTelemetry tracer. Client trace links to server trace via `trace_id`. |
| **Structured logging** | Use MCP `logging` capability — servers send `notifications/message` to client (not stderr). Client routes to existing `RunFileLog`. |
| **Transport debugging** | MCP Inspector for local stdio testing. `--debug` flag on servers enables verbose JSON-RPC logging. |

---

## Testing Strategy

| Layer | Approach |
|-------|----------|
| **Server unit tests** | Each server gets `test_server.py` — calls handlers directly, no transport layer (same as current unit tests). |
| **Transport integration** | MCP Inspector or in-process stdio simulation. Verifies JSON-RPC serialization. |
| **Client adapter tests** | Mock MCP server (in-memory, no subprocess) for graph-level tests. |
| **Contract tests** | Verify each server's `tools/list` output matches expected schemas. Run in CI. |
| **Latency benchmarks** | Phase 1 CI job: measure cold-start + stdio round-trip per domain. Alert if >100ms. |

---

## What Stays the Same

- `ToolDispatcher` (mutation guard, idempotency, batch execution) — just routes through MCP instead of direct handlers
- `ToolSelector` / router — operates on schema names regardless of source
- `validate_entities` node — still checks entity IDs pre-mutation
- `prepare_confirm` / `confirm` / `executor` flow — unchanged, still pauses for risky calls
- Graph topology — no structural changes

---

## What Gets Simpler

| Before | After |
|--------|-------|
| `ToolSpec` + `ToolRegistry` + `DomainAdapter` per domain | One MCP server per domain |
| `registry_factory.py` wiring | Config: `{domain: server_command, credential_mapping}` |
| Manual prompt fragment composition | MCP `prompts/get` → auto-injected |
| Risk metadata in central `metadata.py` table | MCP annotations (`destructiveHint`) co-located with each tool |
| Adding a domain touches 5+ files | One server + one config line |

---

## Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| **Latency from subprocess stdio** | Realistic estimate: 200-400ms cold-start for Python. Mitigate with session-per-conversation (amortized across 3-5 calls). Benchmark in Phase 1; if >100ms overhead per call, implement warm pool before proceeding. |
| **Credential exposure** | Pass via `initializationOptions` (not env vars). Long-term: MCP auth extension or sidecar vault. |
| **MCP SDK maturity** | Pin SDK version explicitly. Test for dependency conflicts (MCP SDK pulls `httpx`, `pydantic`, `anyio`, `sse-starlette` — may conflict with existing starlette pin). Use `FastMCP` for convenience but isolate via the adapter layer. |
| **Debugging across process boundary** | Correlation IDs via tool args, MCP `logging` capability for structured diagnostics, per-server LangSmith traces. |
| **DNS rebinding (Streamable HTTP)** | If/when moving to Streamable HTTP, require Origin header validation and localhost binding. |
| **Graceful shutdown** | Cancel in-flight JSON-RPC, 5s cleanup deadline, SIGKILL fallback. |

---

## Rough Effort Estimate

| Phase | Effort | Can Ship Independently |
|-------|--------|----------------------|
| 1. MCP client adapter + latency benchmark | 2–3 days | Yes (behind `DOMAIN_TRANSPORT` config) |
| 2. Todoist MCP server | 1–2 days | Yes (swap one domain) |
| 3. GCal MCP server | 1–2 days | Yes |
| 4. Remove legacy | 0.5 day | Yes (after 2+3 stable) |
| 5. New domains | ~0.5 day each | Yes |

---

## Open Questions

1. **Warm pool design?** If latency benchmarks exceed budget: how many sessions per domain, how credentials switch between users, how stale sessions are reaped. Defer until Phase 1 benchmarks land.

2. **Community MCP servers?** For some domains (GitHub, Google Drive), community MCP servers exist. Evaluate adopting vs. writing custom — tradeoff is control over error handling, schema quality, and trust model for annotations.

3. **MCP sampling?** MCP allows servers to request LLM completions from the client. Could enable autonomous sub-chains within a domain server. Not needed for Phase 1 but interesting for complex multi-step domain operations.

---

## Review Verdict & Recommendation (2026-07-25)

The phasing (strangler pattern, per-domain `DOMAIN_TRANSPORT` rollback, latency gate) is sound, and the spec corrections above make the design accurate. But the strategic case is thin **right now**:

- The infrastructure being replaced (`base.py` + `domain_adapters.py` + `registry_factory.py`) is ~400 lines of working, tested, in-process code. The replacement adds subprocess lifecycle management, JSON-RPC transport, a private credential extension, a contingent warm-pool design, and known dependency conflicts (sse-starlette vs. the pinned starlette).
- Every coupling point in the "expensive" list can be fixed in-process in ~1 day: self-registering adapters (decorator), and deriving selector/risk tables from `ToolSpec` metadata the way `metadata.py` already does. That delivers the Phase 5 payoff ("one module + one registration line per domain") with zero new processes.
- CLAUDE.md explicitly lists MCP child-process infrastructure as out of scope — this repo already walked away from it once.

**When MCP earns its cost:** adopting community servers (GitHub, Drive, Notion) instead of writing clients ourselves, or when a domain needs independent deployment/scaling. Neither applies with two active domains and two users.

**Recommendation:**
1. Do the in-process decoupling now (~1 day).
2. Keep this doc as the playbook for when a community server is worth adopting — that is the trigger for Phase 1, built on `langchain-mcp-adapters`.
3. If proceeding anyway: the Phase 1 latency benchmark stays a hard merge gate.

4. ~~**`structuredContent` for errors?**~~ Resolved: `structuredContent` is stable in the 2025-06-18 spec and the Python SDK. Design Decision 5 now uses it directly.
