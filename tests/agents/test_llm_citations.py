"""Citation rendering tests."""

import pytest

from agents.agent_api.app.llm.citations import render_url_citations


def test_no_annotations_returns_text_unchanged():
    assert render_url_citations("Hello world.", []) == "Hello world."


def test_citation_marker_replaced_with_link():
    text = "The museum closes at 6 pm [1]."
    annotations = [
        {
            "type": "url_citation",
            "start_index": 26,
            "end_index": 29,
            "url": "https://example.org/hours",
            "title": "Official opening hours",
        }
    ]
    result = render_url_citations(text, annotations)
    assert result == "The museum closes at 6 pm [Official opening hours](https://example.org/hours)."


def test_ordinary_span_gets_link_appended():
    text = "The museum closes at 6 pm."
    annotations = [
        {
            "type": "url_citation",
            "start_index": 0,
            "end_index": 25,
            "url": "https://example.org/hours",
            "title": "Official opening hours",
        }
    ]
    result = render_url_citations(text, annotations)
    assert "[Official opening hours](https://example.org/hours)" in result
    assert result.startswith("The museum closes at 6 pm")


def test_multiple_citations_at_different_claims():
    text = "A is true. B is also true."
    annotations = [
        {
            "type": "url_citation",
            "start_index": 0,
            "end_index": 10,
            "url": "https://a.example.com",
            "title": "Source A",
        },
        {
            "type": "url_citation",
            "start_index": 11,
            "end_index": 26,
            "url": "https://b.example.com",
            "title": "Source B",
        },
    ]
    result = render_url_citations(text, annotations)
    assert "[Source A](https://a.example.com)" in result
    assert "[Source B](https://b.example.com)" in result


def test_duplicate_url_at_same_position_deduplicated():
    text = "Fact [1]."
    annotations = [
        {"type": "url_citation", "start_index": 5, "end_index": 8, "url": "https://x.com", "title": "X"},
        {"type": "url_citation", "start_index": 5, "end_index": 8, "url": "https://x.com", "title": "X"},
    ]
    result = render_url_citations(text, annotations)
    assert result.count("https://x.com") == 1


def test_invalid_offsets_fall_back_to_appended_link():
    text = "Short."
    annotations = [
        {"type": "url_citation", "start_index": 0, "end_index": 999, "url": "https://x.com", "title": "X"},
    ]
    result = render_url_citations(text, annotations)
    assert result.startswith("Short.")
    assert "[X](https://x.com)" in result


def test_boolean_offsets_rejected():
    text = "Hello."
    annotations = [
        {"type": "url_citation", "start_index": True, "end_index": 5, "url": "https://x.com", "title": "X"},
    ]
    result = render_url_citations(text, annotations)
    assert "[X](https://x.com)" in result


def test_non_http_url_skipped():
    text = "Hello."
    annotations = [
        {"type": "url_citation", "start_index": 0, "end_index": 5, "url": "ftp://x.com/file", "title": "X"},
    ]
    assert render_url_citations(text, annotations) == "Hello."


def test_unknown_annotation_type_skipped():
    text = "Hello."
    annotations = [
        {"type": "file_citation", "start_index": 0, "end_index": 5, "url": "https://x.com"},
    ]
    assert render_url_citations(text, annotations) == "Hello."


def test_unicode_before_citations():
    text = "☃ Snow info [1]."
    annotations = [
        {
            "type": "url_citation",
            "start_index": 12,
            "end_index": 15,
            "url": "https://snow.example.com",
            "title": "Snow report",
        }
    ]
    result = render_url_citations(text, annotations)
    assert "☃" in result
    assert "[Snow report](https://snow.example.com)" in result


def test_missing_title_uses_url():
    text = "Fact [1]."
    annotations = [
        {"type": "url_citation", "start_index": 5, "end_index": 8, "url": "https://x.com"},
    ]
    result = render_url_citations(text, annotations)
    assert "[https://x.com](https://x.com)" in result
