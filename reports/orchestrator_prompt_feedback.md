# Orchestrator Prompt Assessment & Recommendations

Last updated: 2026-06-23

## Context

Assessment of whether the current orchestrator prompt (`agents/agent_api/app/graph/prompts/orchestrator.py`) is production-ready, and what gaps exist between the prompt and the actual LangGraph infrastructure.

---

## Verdict: Production-ready for current Todoist-only scope

After implementing the quick wins and timezone injection, the prompt is well-aligned with the infrastructure. Remaining items are medium/larger efforts for when the tool surface grows.

---

## Problems Fixed (items 1-7, implemented)

### 1. ~~Dead Worker/Dispatch References~~ DONE

Preamble rewritten to: "You handle requests by calling tools directly, chaining multiple calls when needed." All worker references removed from failure and limits sections.

### 2. ~~Reasoning Effort Section is Unactionable~~ DONE

Section deleted entirely. DeepSeek's API doesn't expose a reasoning-effort toggle.

### 3. ~~Model Has Zero Awareness of the Confirm Gate~~ DONE

New `## Confirm gate (system-managed)` section added. Model now knows deletions and bulk mutations (5+) are intercepted, and knows not to retry declined actions.

### 4. ~~No Tool-Specific Guidance~~ DONE

New `## Todoist tool tips` section covers: priority inversion, date format preference (`due_string`), filter syntax examples, no-fabricate-IDs rule, no-retry-on-add rule, parallel call guidance.

### 5. ~~No Timezone in Runtime Context~~ DONE

Runtime context now includes `User timezone: {tz}`. Reads from `JARVIS_USER_TIMEZONE` env var (defaults to `Asia/Taipei` in config). Also added to `Settings` dataclass in `config.py`.

### 6. Missing Discovery Tools — OPEN

The model knows about `project_id`, `section_id`, `labels` parameters but has NO tools to look up valid values. It either hallucates IDs or asks the user for information they shouldn't need to provide.

### 7. ~~Grammar Issues~~ DONE

"the Jerry's" fixed to "Jerry's" throughout.

---

## Remaining Recommendations

### Medium Effort (small infra + prompt)

8. **Add `get_projects` and `get_labels` tools** — two simple read-only REST wrappers so the model can discover valid project/label values. Would eliminate a class of hallucination and unnecessary clarification questions.

9. **Fix max-turns behavior** — Current infra hard-stops at turn 8 with an error message. Prompt now says "stop with best partial answer" (aligned). But ideally the infra would trigger graceful wind-down at turn 7 rather than a hard cut at 8.

### Larger Efforts (future features)

10. **Cross-session memory** — User preferences, frequent project IDs, names. Currently every thread is stateless.

11. **Tool output summarization** — `get_tasks` can return 200 tasks as raw JSON, bloating context. A summarization/extraction step before the next agent turn would reduce token cost.

12. **DISPATCH/workers** — Only needed when tool surface grows (Gmail, Calendar, Notion). Prompt aspirational language removed; re-add when workers are implemented.

13. **Streaming partial responses** — For long multi-tool operations, user sees nothing until ANSWER. Intermediate status messages ("Found 12 tasks, filtering...") would improve Telegram UX.

---

## Files Changed

| File | Change |
|------|--------|
| `agents/agent_api/app/graph/prompts/orchestrator.py` | Full prompt rewrite: removed dispatch/worker/reasoning-effort, added confirm gate + tool tips + timezone |
| `agents/agent_api/app/config.py` | Added `user_timezone` setting (default `Asia/Taipei`) |

---

## Verification

- All 77 existing tests pass (`PYTHONPATH=. pytest agents/tests/ -v`)
- Module imports cleanly, `get_orchestrator_prompt()` renders correctly
- Manual test targets:
  - Send "delete my task X" → model should not retry after decline
  - Send "add task for tomorrow at 3pm" → model should use `due_string`
  - Send "show my p1 tasks" → model should use `get_tasks_by_filter` with filter `"p1"`
