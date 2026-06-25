"""Prompt context and message builders shared across roles.

Also home to the (currently unused) ``available_tools_line`` helper: it can render
the orchestrator's "Available tools" line from a :class:`ToolRegistry` so the
prompt stays correct as domains are added. The shipped orchestrator prompt keeps
its static wording for now; wire this in when more domains go live.
"""

from datetime import datetime
from typing import Any, Dict, List

from agents.agent_api.app.graph.prompts.orchestrator import get_system_prompt

USER_PROMPTS: List[str] = [
    # "set a dinner appointment with zac anytime during dinner from 24 June onwards"

    # "add in go home with feebee later i just added"
    "remove the add in go home with feebee later i just added"

    # "meeting zac at night on friday, add it in" # always add it in first, then check for conflicts and report back if conflict else end.
    # "i alr did romans 7 in the train this morning uhm but not romans 8 yet, shift romans 8 to tonight"
    # "Go through my tasks, check everything that does not have a time, that is also not a birthday. Tell me first and then I will ask you to make edits",
    # "put in my cal",
    # "can u add 24 tasks  today each one titled 'hehehehehehehehehe'",
    # "how many hehehe tasks do i have today"
    # "delete all my hehehe tasks today"
    # "Add three tasks for my morning routine.",
    # "Clean up my list.",
    # "Delete all tasks on Tuesday."

    # --- Regression test prompts (test on Telegram) --- Always test this flow...
    # 1. Bulk add (tests bulk_add_todoist_tasks + confirm gate rendering)
    # "add 24 tasks titled 'hehehehehehhe' due tomorrow",
    # 2. Summarizer trigger (tests route_after_tools → summarize node on large results)
    # "show me all my tasks for today and tmr",
    # 3. Summarizer bypass — count query (tests _is_count_query bypass path)
    # "how many tasks do I have due this week?",
    # 4. Concurrent executor + confirm (tests batch confirm rendering + parallel execution)
    # "delete all my hehehehehehhe tasks tmr",
    # 5. Pagination + large result handling (tests cursor null handling + summarizer)
    # "list every task in my inbox",

    # --- Stress test prompts (CEO calendar) ---
    # Convention: RESET to a clean calendar, uncomment ONE case block, run its
    # setup lines first (auto-approve their mutations), then the TEST line under
    # normal gating. Grade the TEST. (8/9/10 also need runner fault injection;
    # 15 is run twice — with and without the Tokyo line.)

    # ── 1 · control_flow / decline_kills_turn ──
    # "Add a budget review this Friday 1pm to 2pm.",
    # "Add a marketing sync this Friday 2:30pm to 3pm.",
    # "Add a vendor call this Friday 4pm to 4:30pm.",
    # "Clear my Friday afternoon and drop a 2-hour strategy block in its place.",  # decline→END kills the whole turn incl. the unobjectionable create

    # ── 2 · control_flow / missing_confirm_to_hitl ──
    # "Add the quarterly board call this Thursday 10am to 12pm.",
    # "Add a 1:1 with Sarah my COO this Thursday 10:30am to 11am.",
    # "Add an analyst briefing this Thursday 11am to 11:30am.",
    # "Add a recruiter intro call this Thursday 11:30am to 12pm.",
    # "Cancel the meetings that clash with Thursday's board call, but not the ones with my direct reports.",  # no confirm→hitl path; agent guesses (destructive) or dies after prepare_confirm

    # ── 4 · confirm_semantics / deferred_sibling_prerequisite ──
    # "Add Partnerships sync (tentative) tomorrow 2pm.",
    # "Add Press interview (tentative) tomorrow 4pm.",
    # "Add Exec staff meeting tomorrow 9am.",
    # "Add Lunch with mentor tomorrow 12pm.",
    # "Pull up tomorrow's schedule and delete anything still marked tentative.",  # prepare_confirm defers the read; deletes freeze from stale context, the read that grounds them runs after

    # ── 5 · loop_limits / turn_guard_partial_mutation ──
    # "Add a leadership sync next Monday 9am.",
    # "Add an investor 1:1 next Wednesday 2pm.",
    # "Add a product review next Friday 11am.",
    # "Add a board prep session in 10 days at 3pm.",
    # "Add an all-hands in 12 days at 4pm.",
    # "Add a customer dinner in 15 days at 7pm.",
    # "Add a finance review in 18 days at 10am.",
    # "Add a strategy offsite in 22 days at 9am.",
    # "Add a recruiting panel in 26 days at 1pm.",
    # "Add a press briefing in 29 days at 5pm.",
    # "Add a 15-minute prep buffer before every meeting I have in the next 30 days.",  # per-task loops trip MAX_AGENT_TURNS=8 → error→END after partial buffers written, no rollback

    # ── 6 · loop_limits / constraint_satisfaction_bulk ──
    # "Add a board 1:1 with director Alice next Tuesday 3pm.",
    # "Add a board 1:1 with director Ben next Thursday 3pm.",
    # "Add a board 1:1 with director Carol next Friday 3pm.",
    # "Add a standup next Monday 9am.",
    # "Add a finance call next Monday 11am.",
    # "Add a legal review next Monday 2pm.",
    # "Book 30-min prep 1:1s the day before each board member meeting, but never on a day I already have 3 or more things.",  # day-before for Tue is Mon (already 3) → must block/relocate; multi-stage compute risks turn guard

    # ── 7 · loop_limits / large_context_grounding ──
    # "Add a strategy offsite next Monday 9am to 5pm.",
    # "Add an investor roadshow next Tuesday 9am to 4pm.",
    # "Add product reviews next Wednesday 10am to 3pm.",
    # "Add board prep next Thursday 9am to 1pm.",
    # "Add a customer summit next Friday 9am to 6pm.",
    # "Add a hiring panel in 5 weeks at 2pm.",
    # "Add a partnerships dinner in 7 weeks at 7pm.",
    # "Add a finance close review in 9 weeks at 10am.",
    # "Give me the whole quarter, every meeting grouped by theme, and flag any week over 20 hours of meetings.",  # context overflow tempts a stale/partial answer; tests turn_grounded + full-set aggregation (next week >20h)

    # ── 8 · freshness / toctou_cascade  (inject a between-confirm-and-execute change) ──
    # "Add an investor call today 3pm to 4pm.",
    # "Add a product demo tomorrow 10am to 10:45am.",
    # "Add an ops review tomorrow 10:30am to 11am.",
    # "Move my 3pm investor call to tomorrow 10am and push anything that now collides with it.",  # held_calls freezes collisions from pre-move state; executor replays stale conflicts

    # ── 9 · freshness / stale_resume_durability  (delay the HITL reply / mutate slot during pause) ──
    # "Add a leadership sync next Monday 9am.",
    # "Add an investor 1:1 next Tuesday 2pm.",
    # "Add a product review next Wednesday 11am.",
    # "Add board prep next Thursday 3pm.",
    # "Find me a 90-minute hold for the offsite next week and lock it in.",  # no stale-resume guard; InMemorySaver loses paused state on restart

    # ── 10 · freshness / idempotency_retry  (no setup; inject a mid-execute failure so retry runs) ──
    # "Set up the recurring Monday exec sync for the rest of the year.",  # no idempotency key → executor partial-failure + retry double-creates

    # ── 11 · reference / positional_against_shown_list  (TWO graded turns) ──
    # "Add a task Review Q3 OKRs with no time, due next Monday.",
    # "Add a task Approve marketing budget with no time, due next Tuesday.",
    # "Add a task Sign vendor contract with no time, due next Wednesday.",
    # "Add a task Prep board deck with no time, due next Thursday.",
    # "Add a task Call recruiter with no time, due next Friday.",
    # "List my tasks that don't have a time.",                                  # turn 1: establishes the order
    # "Give the second and fourth a 9am start and delete the last one.",        # turn 2 (graded): fresh re-fetch reorders → indices map to wrong tasks → destructive mis-reference

    # ── 12 · reference / exclusion_filter_destructive ──
    # "Add a comp committee meeting next Monday 9am.",
    # "Add a marketing sync next Monday 10am.",
    # "Add a legal review next Monday 11am.",
    # "Add a contract legal sign-off next Monday 1pm.",
    # "Add a vendor call next Monday 2pm.",
    # "Add a 1:1 with my CFO next Monday 3pm.",
    # "Cancel everything next Monday except the comp committee meeting and anything legal.",  # negation filter + 'cancel' ambiguity (complete vs delete) + bulk binary confirm

    # ── 13 · reference / recurring_series_vs_instance ──
    # "Add a daily standup every weekday at 9am.",
    # "Add a weekly 1:1 with my COO every Tuesday at 2pm.",
    # "Add a weekly 1:1 with my CTO every Thursday at 2pm.",
    # "Add a board call this Friday 10am.",
    # "I'm out sick all week, clear my standups and 1:1s through Friday.",  # bulk path treats recurring as flat → deletes the whole series or wrong instances; non-recurring board call must survive

    # ── 14 · reasoning / self_contradiction ──
    # "Add a board meeting this Wednesday 10am.",
    # "Add an investor call this Thursday 2pm.",
    # "Add a vendor demo this Monday 3pm.",
    # "Add a newsletter review this Tuesday 11am.",
    # "Add an internal sync this Friday 4pm.",
    # "Free up my entire week for deep work, but don't move anything important.",  # 'free up everything' vs 'keep important' → must ask what's important, not silently pick a definition and bulk-mutate

    # ── 15 · reasoning / conditional_branch_timezone  (run twice; drop the Tokyo line for variant B) ──
    # "Add Tokyo leg investor roadshow next Monday all day.",   # ← omit this line for the FALSE branch
    # "Add an analyst call next Tuesday 9am.",
    # "Add a partner sync next Tuesday 10am.",
    # "Add a team check-in next Tuesday 11am.",
    # "I land in London Tuesday morning. If the Tokyo leg is still on my calendar, leave next week alone; otherwise shift Tuesday's calls to after 2pm local.",  # conditional branch in one turn + '2pm local' TZ conversion; A=no changes, B=shift after 2pm London
    
    # --16 -- stress testing the bulk add of many tasks, and the summarizer for large results, and the count query bypass, and the concurrent executor with confirm, and the pagination with large result handling
    # part a
    # '''Add these 20 different items into my Todoist task calendar:
    #     1. Submit internship timesheet tomorrow at 9am
    #     2. Review fraud detection experiment results tomorrow at 11am
    #     3. Buy groceries tomorrow evening
    #     4. Call Mum on Friday at 8pm
    #     5. Pay credit card bill on Friday
    #     6. Clean my room on Saturday morning
    #     7. Go for a 5km run on Saturday at 5pm
    #     8. Meal prep for the week on Sunday at 3pm
    #     9. Read 2 chapters of ML textbook on Monday at 10am
    #     10. Finish LangGraph checkpointing notes on Monday at 2pm
    #     11. Update GitHub README on Tuesday at 4pm
    #     12. Book dentist appointment on Wednesday morning
    #     13. Prepare slides for project meeting next Thursday at 1pm
    #     14. Meet Feebee for dinner next Thursday at 7pm
    #     15. Renew library books next Friday
    #     16. Plan Korea trip itinerary next Saturday at 2pm
    #     17. Pack passport, adapter, sunscreen, headphones and power bank as separate packing tasks for Korea trip next Sunday morning
    #     18. Check AWS SageMaker costs on the 1st of next month
    #     19. Review monthly budget on the 1st of next month at 9pm
    #     20. Water the plants every Sunday at 10am
    # '''

    
    # part b
    # '''
    # Remove the following items from my Todoist task calendar:
        # 1. Submit internship timesheet
        # 2. Review fraud detection experiment results
        # 3. Buy groceries
        # 4. Call Mum
        # 5. Pay credit card bill
        # 6. Clean my room
        # 7. Go for a 5km run
        # 8. Meal prep for the week
        # 9. Read 2 chapters of ML textbook
        # 10. Finish LangGraph checkpointing notes
        # 11. Update GitHub README
        # 12. Book dentist appointment
        # 13. Prepare slides for project meeting
        # 14. Meet Feebee for dinner
        # 15. Renew library books
        # 16. Plan Korea trip itinerary
        # 17. Pack passport
        # 18. Pack adapter
        # 19. Pack sunscreen
        # 20. Pack headphones
        # 21. Pack power bank
        # 22. Check AWS SageMaker costs
        # 23. Review monthly budget
        # 24. Water the plants
    # '''
    
    # part c
    # '''I need you to organise my next two weeks in Todoist. Add 20 tasks/calendar items across work, errands, health, finance, and personal life. Use the exact due dates/times when I give them, infer sensible task titles, split bundled packing items into separate tasks, and make recurring items recurring.
        # Tasks:
        # - tomorrow 9am submit internship timesheet
        # - tomorrow 11am review fraud detection experiment results
        # - tomorrow evening buy groceries
        # - Friday 8pm call Mum
        # - Friday pay credit card bill
        # - Saturday morning clean my room
        # - Saturday 5pm go for a 5km run
        # - Sunday 3pm meal prep for the week
        # - Monday 10am read 2 chapters of ML textbook
        # - Monday 2pm finish LangGraph checkpointing notes
        # - Tuesday 4pm update GitHub README
        # - Wednesday morning book dentist appointment
        # - next Thursday 1pm prepare slides for project meeting
        # - next Thursday 7pm meet Feebee for dinner
        # - next Friday renew library books
        # - next Saturday 2pm plan Korea trip itinerary
        # - next Sunday morning pack passport, adapter, sunscreen, headphones, and power bank as separate Todoist tasks
        # - 1st of next month check AWS SageMaker costs
        # - 1st of next month 9pm review monthly budget
        # - every Sunday 10am water the plants
    # '''

    
]

USER_PROMPT = USER_PROMPTS[0] if USER_PROMPTS else ""

def available_tools_line(registry: Any) -> str:
    """Render an 'Available tools' line from a registry (for future prompt use)."""

    names = [
        schema.get("function", {}).get("name", "")
        for schema in registry.openai_schemas()
    ]
    names = [name for name in names if name]
    return "Available tools: " + (", ".join(names) if names else "none")


def build_user_prompt_with_request_datetime(user_prompt: str) -> str:
    """Add the current request timestamp to the user message content."""

    return "\n".join(
        [
            "Request context:",
            f"Current request date and time: {datetime.now().astimezone().isoformat(timespec='seconds')}",
            "",
            "User request:",
            user_prompt,
        ]
    )


def build_initial_messages(user_prompt: str) -> List[Dict[str, Any]]:
    """Create the raw message list used by the DeepSeek API."""

    return [
        {"role": "system", "content": get_system_prompt()},
        {"role": "user", "content": build_user_prompt_with_request_datetime(user_prompt)},
    ]


__all__ = [
    "USER_PROMPT",
    "USER_PROMPTS",
    "available_tools_line",
    "build_initial_messages",
    "build_user_prompt_with_request_datetime",
]
