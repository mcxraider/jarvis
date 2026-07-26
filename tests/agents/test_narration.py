from agents.agent_api.app.tracing import TracePrinter, UserProgressTracePrinter


def test_narration_emits_through_callback():
    events = []
    tracer = UserProgressTracePrinter(lambda p: events.append(p))
    tracer.narration("Let me look that up")
    assert events == [{"narration": "Let me look that up"}]


def test_narration_skips_empty():
    events = []
    tracer = UserProgressTracePrinter(lambda p: events.append(p))
    tracer.narration("")
    tracer.narration("   ")
    assert events == []


def test_narration_skips_none():
    events = []
    tracer = UserProgressTracePrinter(lambda p: events.append(p))
    tracer.narration(None)
    assert events == []


def test_base_tracer_narration_is_noop():
    tracer = TracePrinter(enabled=True, show_payloads=False)
    tracer.narration("hello")  # should not raise
