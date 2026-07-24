# MCP Migration Design Document

**Protocol Version:** MCP 2025-06-18 spec

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
- Owns its own credential handling (passed via `initializationOptions` during MCP init handshake)
- Declares tool metadata: `name` (identifier), `title` (human-readable display), description, `inputSchema`, `outputSchema` (typed results), and annotations (`readOnlyHint`/`destructiveHint`/`idempotentHint`/`openWorldHint`)
- Exposes **prompts** (replaces `prompt_fragment`) and **resources** (replaces grounding notes)
- Validates all inputs at its own boundary (existing Pydantic schemas move into the server)

---

## MCP Primitives Usage

The migration uses all three MCP server primitives — not just tools.

| Primitive | Current Jarvis Feature | MCP Mapping |
|-----------|----------------------|-------------|
| **Tools** | `ToolSpec` handlers | `tools/list` + `tools/call` — the core migration |
| **Resources** | `GROUNDING_NOTE` per domain | `resources/list` with `audience: ["assistant"]` — client auto-injects into context |
| **Resources** | Entity grounding (project names, calendar IDs) | Subscribable resources — client pre-fetches without a tool call |
| **Prompts** | `PROMPT_FRAGMENT` per domain | `prompts/get` with domain name — structured messages prepended to system prompt |

Phase 2 defines which domain context moves to resources vs. prompts vs. stays as tool descriptions.

---

## Migration Plan

### Phase 1: MCP Client Layer in LangGraph

**What:** Add an MCP client adapter that presents MCP-discovered tools as the existing `ToolSpec` interface, so the rest of the graph doesn't change yet.

**Files to create:**
- `agents/agent_api/app/tools/mcp_adapter.py` — wraps `mcp` SDK client, converts MCP tool schemas → `ToolSpec`, routes calls through MCP `call_tool`

**Key decisions:**
- Transport: **stdio** for co-located servers (simplest, no network). Keeps deployment identical — one container, child processes.
- Session lifecycle: **one MCP session per (user, domain, conversation)**. Spawn on first tool call for that domain, keep alive for the conversation duration (matches `RuntimeContextSnapshot` lifecycle). Reap stale sessions after conversation timeout.
- The adapter reads MCP tool annotations to populate `mutating` flag (`readOnlyHint: false` → mutating). Note: annotations are "hints" per spec — trusted here because we own all servers.
- The adapter reads `outputSchema` from tools to get typed results (improvement over current arbitrary dict returns).
- Credential injection: pass via `initializationOptions` in the MCP init handshake (not env vars — avoids `/proc/PID/environ` exposure on shared hosts).

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

**Credential passing:** Orchestrator passes credential in `initializationOptions` during MCP session init. Server reads from init params, not env vars.

### Phase 3: Convert Google Calendar to MCP Server

Same pattern. Token rotation: server emits `notifications/resources/updated` when refresh tokens rotate. Client subscribes and persists the new token back to credential store. (Avoids the awkward "callback URL or direct DB write" problem — the MCP notification protocol handles it cleanly.)

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
- **Credential rotation mid-session:** Server emits `notifications/resources/updated`. Client persists rotated token.
- **Graceful shutdown:** In-flight `call_tool` gets a JSON-RPC cancel. Server has 5s to clean up before SIGKILL.

### 2. Credential Isolation

Credentials via `initializationOptions` (not env vars):
- Avoids `/proc/PID/environ` exposure
- Supports credential rotation without process restart
- Clean separation: orchestrator owns credential resolution, server owns API calls

### 3. Risk Classification via Annotations

MCP tool annotations replace hardcoded `graph/risk.py` lists:
- `destructiveHint: true` → risky, requires HITL confirmation
- `readOnlyHint: true` → safe, no confirmation needed
- `idempotentHint: true` → safe for retry

**Trust model:** Since we own all servers, annotations are trusted. If adopting third-party MCP servers in future, treat annotations as untrusted and maintain an override list.

### 4. Idempotency

Stays at the dispatcher level (MCP client side). The MCP server is a stateless executor; deduplication stays in the orchestrator where it has thread/turn context.

### 5. Error Contract

MCP servers return structured errors inside the `content` text field (with `isError: true`):

```json
{
  "error_class": "rate_limited",
  "retryable": true,
  "retry_after_seconds": 30,
  "message": "Todoist API rate limit exceeded"
}
```

The client adapter parses this JSON and maps to `ClassifiedApiError` (retryable / ambiguous / permanent). Protocol-level JSON-RPC errors (unknown tool, invalid args) are treated as permanent/non-retryable.

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
| Risk classification hardcoded per tool name | MCP annotations (`destructiveHint`) |
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

4. **`structuredContent` for errors?** Alternative to JSON-in-text: use MCP's `outputSchema` + `structuredContent` for typed error responses. Evaluate when SDK support stabilizes.
