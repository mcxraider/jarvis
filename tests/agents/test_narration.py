from agents.agent_api.app.tracing import TracePrinter, UserProgressTracePrinter


def test_reasoning_summary_emits_through_callback():
    events = []
    tracer = UserProgressTracePrinter(lambda p: events.append(p))
    tracer.reasoning_summary("Let me look that up")
    assert events == [{"reasoning_summary": "Let me look that up"}]


def test_reasoning_summary_skips_empty():
    events = []
    tracer = UserProgressTracePrinter(lambda p: events.append(p))
    tracer.reasoning_summary("")
    tracer.reasoning_summary("   ")
    assert events == []


def test_reasoning_summary_skips_none():
    events = []
    tracer = UserProgressTracePrinter(lambda p: events.append(p))
    tracer.reasoning_summary(None)
    assert events == []


def test_base_tracer_reasoning_summary_is_noop():
    tracer = TracePrinter(enabled=True, show_payloads=False)
    tracer.reasoning_summary("hello")  # should not raise
