# Combined router eval findings

Source reports: `avery`, `jerry`, `marcus`, `nadia`, `phoebe`, and `zac` evals from `2026-07-07T17:22:25Z`.

## Executive summary

The pre-orchestrator router is broadly reliable for domain selection across the six evaluated personas. The strongest runs are `phoebe` and `zac`, which both achieve perfect raw policy matches. `avery` also has perfect raw routing, but its final guarded result regresses one unsupported-domain query. `nadia` has one raw false negative on reminder language. `jerry` and `marcus` show the most interesting behavior because they use Todoist as the generic event/schedule provider and Google Calendar as explicit-only: the raw router is mostly correct, and the guardrail layer fixes some Google Calendar over-routes, but it also creates a high-risk unsupported-domain false positive.

The main recurring issues are:

- Guardrails can turn unsupported-provider requests into Todoist routes.
- Reminder language needs stronger routing coverage.
- Todoist-first explicit-only calendar policies work, but need careful precision around explicit `google cal` / `gcal` mentions.
- Rewrites sometimes narrow, over-specify, or drop useful user intent.
- Latency is acceptable but near the edge for a router that runs before every assistant turn.

## Persona scorecard

| Persona | Policy shape | Raw match | Final match | Main issue |
| --- | --- | ---: | ---: | --- |
| Avery | Todoist handles tasks and generic schedule/calendar; Google Calendar explicit-only; both connected | 21 / 21 | 20 / 21 | Guardrail changes unsupported Notion request from `[]` to `todoist` |
| Jerry | Todoist handles tasks and generic schedule/calendar; Google Calendar explicit-only; both connected | 19 / 21 | 20 / 21 | Raw over-routes mixed event phrasing; guardrail fixes those but creates Notion false positive |
| Marcus | Todoist handles tasks and generic schedule/calendar; Google Calendar explicit-only; Google Calendar disconnected | 20 / 21 | 19 / 21 | Explicit `google cal` query over-routes to both domains; guardrail Notion false positive |
| Nadia | Todoist tasks, Google Calendar events; both connected | 20 / 21 | not reported | `remind me...` returns no domain instead of Todoist |
| Phoebe | Todoist tasks, Google Calendar events; only Google Calendar connected | 21 / 21 | not reported | No routing miss; downstream ambiguity and rewrite fidelity are the main risks |
| Zac | Todoist tasks, Google Calendar events; both connected | 21 / 21 | not reported | No routing miss; rewrite fidelity is the main risk |

## Cross-persona findings

### 1. Raw domain routing is strong overall

The router consistently separates task/project/Todoist requests from calendar/schedule/availability requests when the persona policy is straightforward (`nadia`, `phoebe`, `zac`). It also detects explicit mixed task-plus-calendar requests, especially the `tasks and meetings both` and `todos` plus `booked on my cal` cases.

For `phoebe` and `zac`, all 21 raw decisions match the written policy. `avery` also reaches 21 / 21 raw matches. The remaining misses are narrow: `nadia` under-routes a reminder, while `jerry` and `marcus` have precision issues around Todoist-first explicit-only calendar routing.

### 2. Guardrails are the largest correctness risk

The repeated high-risk failure is the unsupported Notion query:

`check my notion page for meeting notes from yesterday`

Raw routing correctly returns `[]` in the relevant reports, but guardrails sometimes coerce it into `["todoist"]` because the query contains schedule-like words such as `meeting` and date language such as `yesterday`. This happens in `avery`, `jerry`, and `marcus`.

That behavior is worse than leaving the domains empty: it exposes an unrelated provider, can trigger irrelevant tool planning, and may produce confusing answers for an unavailable service.

### 3. Reminder routing needs explicit coverage

`nadia` misses:

`remind me to text mom back later today`

Expected behavior is `["todoist"]`, but the raw router returns `[]`. Other personas generally route reminder/task creation correctly, so this looks fixable through prompt coverage and regression fixtures. The Todoist domain description and rules should explicitly include `reminders`, not only tasks, to-dos, and projects.

### 4. Todoist-first calendar policies mostly work

For `avery`, `jerry`, and `marcus`, generic schedule, calendar, free/busy, and availability wording should route to Todoist unless Google Calendar is explicitly named. The router generally follows that contract.

Examples that correctly stay on Todoist under these personas include:

- `when am i free next week?`
- `Go through my schedule...`
- `put in my cal`
- `block off 2hrs friday afternoon for deep work`
- `which one should zac and i grab dinner`

These can look counterintuitive from a product perspective, but they are correct under the written persona policy. If real product behavior should use Google Calendar for generic availability and blocking, the prompt contract itself needs to change.

### 5. Explicit provider intent is usually respected

The router handles explicit `google cal`, `gcal`, and `google calendar` mentions well for default-calendar personas, and mostly well for Todoist-first personas. `marcus` has one precision miss where:

`check my google cal for thursday plz`

routes to both `google_calendar` and `todoist` instead of only `google_calendar`.

Negative provider constraints also work well. The query:

`dont add this to my calendar just keep it on my todo list, dentist appointment reminder`

correctly routes to Todoist only in the evaluated reports.

### 6. Multi-domain recall is good

Across personas, the router reliably widens to both domains when the user explicitly asks for task and calendar context. The recurring examples are:

- `tasks and meetings both`
- `check both my todos and whats already booked on my cal`

This is a strong behavior because it gives the orchestrator enough context to gather both task and event data instead of collapsing the request to a single provider.

### 7. Unsupported and no-op raw classifications are good

The raw router usually returns `[]` for unsupported services (`notion`, `email`) and conversational/no-op turns (`hey u there`, `lol nvm`). This is especially important because unsupported requests and conversational empties are both empty-domain cases, but they need different downstream handling. The raw model is mostly capable here; guardrails should preserve that behavior.

### 8. Query rewrite fidelity is the main non-routing quality risk

Rewrites are often helpful, but several reports identify fidelity issues:

- Temporal weakening: `later today` becomes `today`.
- Over-specific event inference: `put in my cal` becomes `Add an event to my Google Calendar`.
- Advisory request narrowing: `which one should zac and i grab dinner` becomes an availability check.
- Calendar inference: `everything that does not have a time` becomes `all-day events`.

The domain decisions remain mostly correct, but downstream tools should preserve the original user query alongside the rewrite. Evals should score rewrite fidelity separately from domain correctness.

### 9. Schema compliance is mostly good, but reasoning length drifts

JSON/schema validity is reported as perfect in the relevant runs. However, `jerry` and `avery` note that `reasoning` sometimes exceeds the requested 10-word limit. This is low operational risk if the field is diagnostic only, but either the schema should enforce it or the prompt should stop asking for a strict word limit.

## Latency findings

Router latency is usable but not especially fast for a pre-orchestrator step.

| Persona | Median | Average/mean | p95 | Max | Notes |
| --- | ---: | ---: | ---: | ---: | --- |
| Avery | 1008 ms | 1015 ms | 1271 ms | 1422 ms | More than half at or above one second |
| Jerry | 987 ms | 1012 ms | 1223 ms | 1539 ms | Highest max latency in the combined set |
| Marcus | 959 ms | 975 ms | 1149 ms | 1209 ms | Tight distribution, no large outliers |
| Nadia | 1038 ms | 993 ms | 1131 ms | 1243 ms | Median just over one second |
| Phoebe | 880 ms | 928 ms | 1195 ms | 1238 ms | Best median; 16 / 21 under one second |
| Zac | 964 ms | 983 ms | 1276 ms | 1288 ms | Acceptable, but 9 / 21 at or above one second |

The slowest cases are not consistently the hardest semantic cases, and failures do not appear tied to higher latency. The cost looks more like model/prompt overhead than reasoning complexity.

## Recommendations

1. Fix guardrail fallback behavior so explicit unsupported-provider requests remain `[]`.
   - Preserve empty raw decisions when the query is anchored to unsupported services such as `notion`, `email`, `gmail`, `slack`, or `docs`.
   - Only add a fallback domain when there is evidence for a supported provider, not merely a schedule-like word.

2. Add `reminders` to Todoist routing rules and fixtures.
   - Include variants such as `remind me to...`, `later today`, `tomorrow`, `next week`, and reminder requests with explicit calendar rejection.

3. Add rewrite fidelity assertions (into the prompt).
   - Rewrites should not drop timing modifiers, turn recommendations into pure lookups, add missing event details, or convert user wording into stronger assumptions.
   - Keep the original query available to downstream planning even when a rewrite exists.

4. add an unsure payload. So if the model is unsure, itll just load in all the domains under the "unsure" key. basicaly if unsure is empty, add in normal domains. if unsurenot empy, load in unsure domains. or maybe have an unsure boolean, and a most_likely key as well. so if model is unsure, then fallback to most_likely. idk is this a good design choice? 

## Overall verdict

The router is close to production-ready for domain selection, especially for straightforward Todoist-plus-Google-Calendar configurations. The most important fixes are outside the raw classifier: guardrails must stop coercing unsupported-provider requests into Todoist, and the eval harness should separately score raw routing, final routing, and rewrite fidelity. After that, the next gains are reminder coverage and latency reduction.

