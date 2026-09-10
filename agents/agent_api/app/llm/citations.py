"""Render OpenAI url_citation annotations as Markdown links."""

from typing import Any, Mapping, Sequence
from urllib.parse import urlparse


def render_url_citations(text: str, annotations: Sequence[Mapping[str, Any]]) -> str:
    """Convert url_citation annotations into inline Markdown links.

    Edits are applied right-to-left so earlier insertions don't shift later
    offsets. Malformed annotations are silently skipped.
    """
    if not annotations:
        return text

    edits: list[tuple[int, int, str]] = []
    for ann in annotations:
        if ann.get("type") != "url_citation":
            continue
        url = ann.get("url")
        if not isinstance(url, str) or not _safe_url(url):
            continue
        start = ann.get("start_index")
        end = ann.get("end_index")
        if not _valid_offsets(start, end, len(text)):
            title = ann.get("title") or url
            edits.append((len(text), len(text), f" [{title}]({url})"))
            continue
        title = ann.get("title") or url
        span = text[start:end]
        if _is_citation_marker(span):
            edits.append((start, end, f"[{title}]({url})"))
        else:
            edits.append((end, end, f" [{title}]({url})"))

    edits.sort(key=lambda e: (-e[0], -e[1]))
    _deduplicate_edits(edits)

    parts: list[str] = []
    cursor = len(text)
    for start, end, replacement in edits:
        parts.append(text[end:cursor])
        parts.append(replacement)
        cursor = start
    parts.append(text[:cursor])
    parts.reverse()
    return "".join(parts)


def _safe_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _valid_offsets(start: Any, end: Any, length: int) -> bool:
    if isinstance(start, bool) or isinstance(end, bool):
        return False
    if not isinstance(start, int) or not isinstance(end, int):
        return False
    return 0 <= start <= end <= length


def _is_citation_marker(span: str) -> bool:
    stripped = span.strip()
    if not stripped:
        return False
    if stripped.startswith("[") and stripped.endswith("]"):
        return True
    if stripped.startswith("(") and stripped.endswith(")"):
        return True
    return False


def _deduplicate_edits(edits: list[tuple[int, int, str]]) -> None:
    """Remove duplicate URL insertions at the same position (in-place)."""
    seen: set[tuple[int, str]] = set()
    i = 0
    while i < len(edits):
        start, _, replacement = edits[i]
        key = (start, replacement)
        if key in seen:
            edits.pop(i)
        else:
            seen.add(key)
            i += 1
