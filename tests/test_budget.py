from __future__ import annotations

import pytest

from mcp_aws.budget import Cursor, Origin, trim
from mcp_aws.errors import McpAwsError


def _origins(pages: list[tuple[str | None, int]]) -> list[Origin]:
    return [Origin(page_token=token, index=index) for token, index in pages]


def test_cursor_round_trip():
    cursor = Cursor(page_token="abc123", skip=7)
    assert Cursor.decode(cursor.encode()) == cursor


def test_cursor_decode_empty_is_start():
    assert Cursor.decode(None) == Cursor(page_token=None, skip=0)
    assert Cursor.decode("") == Cursor(page_token=None, skip=0)


def test_cursor_decode_rejects_garbage():
    with pytest.raises(McpAwsError, match="Malformed cursor"):
        Cursor.decode("not-a-cursor!!")


def test_trim_under_budget_returns_everything():
    items = [{"i": n} for n in range(5)]
    result = trim(items, _origins([(None, n) for n in range(5)]), max_items=10, max_chars=10_000)
    assert result.items == items
    assert result.truncated is False
    assert result.next_cursor is None


def test_trim_cuts_mid_page_and_resumes_exactly():
    # Two AWS pages of three items each; the cap falls inside the second page.
    items = [{"i": n} for n in range(6)]
    origins = _origins([(None, 0), (None, 1), (None, 2), ("tok2", 0), ("tok2", 1), ("tok2", 2)])

    result = trim(items, origins, max_items=4, max_chars=10_000)

    assert result.items == items[:4]
    assert result.truncated is True
    # The first dropped item is index 1 of the page starting at tok2.
    assert Cursor.decode(result.next_cursor) == Cursor(page_token="tok2", skip=1)


def test_trim_uses_more_pages_token_when_nothing_dropped_locally():
    items = [{"i": n} for n in range(3)]
    result = trim(
        items,
        _origins([(None, n) for n in range(3)]),
        max_items=10,
        max_chars=10_000,
        more_pages_token="next-page",
    )
    assert result.items == items
    assert result.truncated is True
    assert Cursor.decode(result.next_cursor) == Cursor(page_token="next-page", skip=0)


def test_trim_respects_char_budget():
    items = [{"blob": "x" * 200} for _ in range(50)]
    result = trim(items, _origins([(None, n) for n in range(50)]), max_items=50, max_chars=1_000)
    assert 0 < len(result.items) < 50
    assert result.truncated is True
    assert Cursor.decode(result.next_cursor).skip == len(result.items)


def test_trim_always_returns_at_least_one_item():
    """A single oversized item must still come back, or the caller can never advance."""
    items = [{"blob": "x" * 5_000}]
    result = trim(items, _origins([(None, 0)]), max_items=10, max_chars=100)
    assert len(result.items) == 1
    assert result.truncated is False
