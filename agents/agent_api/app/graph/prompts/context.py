"""Prompt context and message builders shared across roles.

The "Available tools" line is rendered by the orchestrator from the runtime
snapshot's registered tool names (or an explicit ``registered_tools`` list for
offline/DI runs), so the prompt's capability claims always match the live
:class:`ToolRegistry`.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from agents.agent_api.app.graph.prompts.orchestrator import (
    _current_user_datetime,
    _user_timezone,
    get_system_prompt,
)
from agents.agent_api.app.user_context.runtime import RuntimeContextSnapshot

USER_PROMPTS: List[str] = [
    "check my calendar and the fact that i have 14 days of leave, figure out the maximum amount of time that i can go overseas for, including weekends, optimise for public holidays."
    # --- Date-range regression prompts (verify year-inclusive filter dates) ---
    # "Show me what's on my plate for this week and next week.",
    # "What tasks do I have due next week?",
    # "Give me a summary of everything due between now and end of next week.",

    # "pull my events from my govtech google calendar and then update lunch with grandparents on todoist to p3 task."
    
    # "Find the plans i have with feebee in the next 2 weeks, put them into google calendar events, then add to todoist a new dinner with feebee church frien ds next saturday."
    
    # "Find all my Todoist tasks and Google Calendar events involving Zac next week. Move anything after 6pm to the earliest free afternoon slot that same week, and mark the related Todoist tasks as P2.",

    # "Find all my plans with Feebee, Zac, and church friends across Todoist and Google Calendar for the next 3 weeks. If any Todoist-only plans have a clear date/time, create matching Google Calendar events. If any Calendar events don’t have Todoist prep tasks, add one 2 hours before. Avoid duplicates.",
    
    # "I want to host dinner next Saturday or Sunday evening with Feebee, Zac, and church friends. Check my calendar and Todoist for conflicts, choose the least busy evening, create the Google Calendar event, then add Todoist tasks to book a place, message everyone, and buy a gift if there is already a birthday-related task nearby.",
    
    # "Clean up next week: find all Todoist tasks that look like meetings, dinners, calls, or appointments. For anything with a specific time but no matching Google Calendar event, create one. For anything with no time, schedule it into my earliest free slot, but don’t schedule social plans before 6pm.",
    
    # "Find the latest dinner/lunch/meetup plans involving grandparents, Feebee, or Zac. If any are overdue Todoist tasks, reschedule them to the next available weekend slot. If any conflict with existing Google Calendar events, move them to the nearest free evening and update both Todoist and Calendar consistently.",
    
    # "Plan my Friday: look at Todoist and Google Calendar, move any flexible tasks to free gaps, keep fixed calendar events untouched, add a 30-minute buffer before every travel/social event, and create one final Todoist task called ‘Friday plan confirmed’ after everything is organized.",
    
    # "Check when I’m free next Monday to Wednesday after work, schedule a 1-hour gym session on the least busy day, then add a Todoist task 30 minutes before it to pack gym clothes.",
    
    # "I think I have two lunch plans with grandparents somewhere in Todoist or Google Calendar. Find the latest one, cancel the older duplicate, and make sure the remaining one is marked important.",
    
    # "Find everything overdue in Todoist that looks like a social plan, move them to this weekend if I’m free, and put the confirmed ones into Google Calendar.",
    
    # "For every event this week that includes dinner in todoist, create a Google calendar reminder 2 hours before it, unless one already exists.",
    
    # "Add dinner with Feebee and church friends next Saturday evening, but only if I don’t already have plans with Feebee that day. If I do, combine them into one calendar event and update the Todoist task title accordingly.",

    # "Whats on my google cal tmr"
    # """### Created tasks

    # | Time | Task | Details |
    # |------|------|---------|
    # | **5:30–6:00 PM** | 🎒 **Pack gym clothes** | Reminder to prepare before heading out |
    # | **6:00–7:00 PM** | 🏋️ **Gym session** | 1-hour workout (duration set) |
    # delete these tasks"""

    
    # """"
    # ## Google Calendar — 9 feebee plans created
    # | Date | Time | Event |
    # |---|---|---|
    # | Jul 12 (Sun) | All day | bring feebee to church |
    # | Jul 12 (Sun) | 12:00–13:00 | Lunch with feebee |
    # | Jul 14 (Tue) | 19:00–21:00 | meet feebee for dinner |
    # | Jul 17 (Fri) | 09:00–21:00 | WFH w feebee ⚠️ |
    # | Jul 18 (Sat) | 15:00–15:30 | pick up feebee |
    # | Jul 18 (Sat) | 16:00–18:00 | go feebee church |
    # | Jul 25 (Sat) | 19:00–21:00 | car picnic with feebee |
    # | Aug 1 (Sat) | 19:00–20:00 | talk with feebee |
    # | Aug 5 (Wed) | 18:00–20:30 | Spiderman brand new day with feebee | 

    # add these from my google calendar.
    # """
    
    # "help me find out what projects i have in todoist, ill ask u to add a task to a one after that"
    # "set a dinner appointment with zac anytime during dinner next week at earliest available date. propose 3 dates and rank them in order of priority"
    # rity based on when im most free"
    # "check phoebe google calendar and my calendar tell me when good day to have dinner with her next week"
    # "i have an AG retreat from 4-6 sept add it in, p1 whole day event."
    # "when am i free next week?"
    # "delete my dinner with zac in my cal monday 8pm"
    # "meeting zac at night on friday, add it in" # always add it in first, then check for conflicts and report back if conflict else end.
    # "i alr did romans 7 in the train this morning uhm but not romans 8 yet, shift romans 8 to tonight"
    # "Go through my tasks, check everything that does not have a time, that is also not a birthday. Tell me first and then I will ask you to make edits",
    # "put in my cal",
    # "can u add 24 tasks  today e
    # ach one titled 'hehehehehehehehehe'",
    # "whats on my cal for this week?"
    # "how many tasks do i have today"
    # "delete all my hehehe tasks today"
    # "Add three tasks for my morning routine.",
    # "Clean up my list.",
    # "Delete all tasks on Tuesday."

    # --- Regression test prompts (test on Telegram) --- Always test this flow...
    # 1. Multi-add (tests the 5+ mutation confirm gate)
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
    
    # --16 -- stress testing multi-add, large-result summarization, count-query bypass, concurrent execution with confirmation, and pagination
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


def build_user_prompt_with_request_datetime(
    user_prompt: str,
    timezone: Optional[str] = None,
    request_datetime: Optional[datetime] = None,
    reply_context: Optional[dict] = None,
) -> str:
    """Add one timezone-resolved request timestamp to the user message content."""

    current = request_datetime or _current_user_datetime(_user_timezone(timezone))

    lines = [
        f"Current datetime: {current.isoformat(timespec='seconds')}",
        f"Current day: {current:%A}",
        "",
    ]
    if reply_context:
        lines += [
            "Reply context:",
            f"- Replied-to role: {reply_context['role']}",
            f"- Replied-to message: {reply_context['message']}",
            "",
        ]
    lines += [
        "Current user message:",
        user_prompt,
    ]
    return "\n".join(lines)


def build_initial_messages(
    user_prompt: str,
    timezone: Optional[str] = None,
    user_name: Optional[str] = None,
    runtime_context: Optional[RuntimeContextSnapshot] = None,
    registered_tools: Optional[List[str]] = None,
    relevant_domains: Optional[Set[str]] = None,
    reply_context: Optional[dict] = None,
) -> List[Dict[str, Any]]:
    """Create the canonical message list used by the selected LLM provider.

    ``relevant_domains`` (from the query router) is forwarded to the system
    prompt to slim the per-domain fragments; ``None`` keeps every active domain
    (today's behavior).
    """

    return [
        {
            "role": "system",
            "content": get_system_prompt(
                timezone,
                user_name=user_name,
                runtime_context=runtime_context,
                registered_tools=registered_tools,
                relevant_domains=relevant_domains,
            ),
        },
        {
            "role": "user",
            "content": build_user_prompt_with_request_datetime(
                user_prompt,
                timezone=(runtime_context.timezone if runtime_context is not None else timezone),
                reply_context=reply_context,
            ),
        },
    ]


__all__ = [
    "USER_PROMPT",
    "USER_PROMPTS",
    "build_initial_messages",
    "build_user_prompt_with_request_datetime",
]
