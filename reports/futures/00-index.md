# Jarvis Future Enhancements — Index

The original `FUTURE_ENHANCEMENTS.md` has been broken into focused files. Each file is self-contained.

| File | Theme | Priority | Status |
|---|---|---|---|
| [01-foundation.md](01-foundation.md) | Core infrastructure before production | Highest | ✅ Done |
| [02-safety-integrity.md](02-safety-integrity.md) | Idempotency, injection, mutation gates, RBAC | High | 🟡 Partial |
| [03-agent-intelligence.md](03-agent-intelligence.md) | Planning, verification, tool selection, reference resolution | High | 🟡 Partial |
| [04-reliability.md](04-reliability.md) | LLM resilience, error taxonomy, retries, batching | High | 🟡 Partial |
| [05-ux.md](05-ux.md) | Progress messages, conversation gating, NL features | Medium | 🟡 Partial |
| [06-observability.md](06-observability.md) | Metrics, eval harness, testing strategy | Medium | 🟡 Partial |
| [07-future-scope.md](07-future-scope.md) | Scheduled jobs, calendar, voice, parallel fan-out, user onboarding | Later | ❌ Not started |
| [dynamic-connector-onboarding.md](dynamic-connector-onboarding.md) | Self-service multi-user onboarding, connector registry, dynamic tool resolution | Later | ❌ Not started |
| [08-open-decisions.md](08-open-decisions.md) | Unresolved product questions | Ongoing | — |

## Progress Summary (as of 2026-06-24)

**Done:** Foundation (API, checkpointing, boundary contract, HITL expiry), LLM resilience, Todoist retry/error taxonomy, destructive-action approval gate, tool output summarization, progress messages.

**Partially done:** Idempotency (key computed, not enforced), tool selection (stub ready), error recovery routing, context-window management, HITL gating, observability.

**Not started:** Safety layer, anti-jailbreak, verification step, reference resolution, scheduled jobs, calendar, multi-user, bulk ops, daily planning.

## Suggested Implementation Order

1. ~~Foundation (01) — nothing else runs without this~~ ✅
2. Safety + Integrity (02) — data corruption is the worst failure mode
3. Agent Intelligence (03) — correctness of the core loop
4. Reliability (04) — resilience around the correct loop
5. Observability (06) — make the system debuggable before scaling
6. UX (05) — user-facing polish
7. Future Scope (07) — expansion after the core is solid
