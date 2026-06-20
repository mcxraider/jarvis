# Jarvis Future Enhancements — Index

The original `FUTURE_ENHANCEMENTS.md` has been broken into focused files. Each file is self-contained.

| File | Theme | Priority |
|---|---|---|
| [01-foundation.md](01-foundation.md) | Core infrastructure before production | Highest |
| [02-safety-integrity.md](02-safety-integrity.md) | Idempotency, injection, mutation gates, RBAC | High |
| [03-agent-intelligence.md](03-agent-intelligence.md) | Planning, verification, tool selection, reference resolution | High |
| [04-reliability.md](04-reliability.md) | LLM resilience, error taxonomy, retries, batching | High |
| [05-ux.md](05-ux.md) | Progress messages, conversation gating, NL features | Medium |
| [06-observability.md](06-observability.md) | Metrics, eval harness, testing strategy | Medium |
| [07-future-scope.md](07-future-scope.md) | Scheduled jobs, calendar, voice, parallel fan-out | Later |
| [08-open-decisions.md](08-open-decisions.md) | Unresolved product questions | Ongoing |

## Suggested Implementation Order

1. Foundation (01) — nothing else runs without this
2. Safety + Integrity (02) — data corruption is the worst failure mode
3. Agent Intelligence (03) — correctness of the core loop
4. Reliability (04) — resilience around the correct loop
5. Observability (06) — make the system debuggable before scaling
6. UX (05) — user-facing polish
7. Future Scope (07) — expansion after the core is solid
