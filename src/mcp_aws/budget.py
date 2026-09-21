"""Response size caps and exact pagination cursors.

Every result is capped twice: by item count and by serialized size. Truncation has to
stay *lossless across calls*, so each item carries its origin — the paginator token for
the page it came from, plus its index within that page. Cutting the list at any point
therefore yields a cursor that resumes at exactly the first dropped item, even when the
cut lands in the middle of an AWS page.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any, NamedTuple

from .errors import McpAwsError


class Origin(NamedTuple):
    """Where an item came from: the token starting its page, and its index in that page."""

    page_token: str | None
    index: int


@dataclass(frozen=True)
class Cursor:
    page_token: str | None
    skip: int

    def encode(self) -> str:
        raw = json.dumps({"t": self.page_token, "s": self.skip}, separators=(",", ":"))
        return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")

    @classmethod
    def decode(cls, value: str | None) -> "Cursor":
        if not value:
            return cls(page_token=None, skip=0)
        try:
            padded = value + "=" * (-len(value) % 4)
            data = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
            return cls(page_token=data.get("t"), skip=int(data.get("s", 0)))
        except Exception as exc:  # noqa: BLE001 - any malformed cursor is a caller error
            raise McpAwsError(
                f"Malformed cursor {value!r}: {exc}. Re-run the query without a cursor."
            ) from exc


@dataclass
class Trimmed:
    items: list[Any]
    truncated: bool
    next_cursor: str | None


def _serialized_len(items: list[Any]) -> int:
    return len(json.dumps(items, separators=(",", ":"), default=str))


def _fit_to_chars(items: list[Any], max_chars: int) -> int:
    """Largest prefix length whose serialization fits in max_chars."""
    if _serialized_len(items) <= max_chars:
        return len(items)
    low, high = 0, len(items)
    while low < high:
        mid = (low + high + 1) // 2
        if _serialized_len(items[:mid]) <= max_chars:
            low = mid
        else:
            high = mid - 1
    return low


def trim(
    items: list[Any],
    origins: list[Origin],
    *,
    max_items: int,
    max_chars: int,
    more_pages_token: str | None = None,
) -> Trimmed:
    """Cap `items` and derive the cursor that resumes exactly where we stopped.

    `more_pages_token` is the paginator's resume token when AWS still had pages left
    after we stopped fetching; it is used only when nothing was dropped locally.
    """
    keep = min(len(items), max_items)
    keep = min(keep, _fit_to_chars(items[:keep], max_chars))

    # A single item over the char budget still has to be returned, or the caller can
    # never make progress past it.
    if keep == 0 and items:
        keep = 1

    if keep < len(items):
        origin = origins[keep]
        return Trimmed(
            items=items[:keep],
            truncated=True,
            next_cursor=Cursor(page_token=origin.page_token, skip=origin.index).encode(),
        )

    if more_pages_token:
        return Trimmed(
            items=items,
            truncated=True,
            next_cursor=Cursor(page_token=more_pages_token, skip=0).encode(),
        )

    return Trimmed(items=items, truncated=False, next_cursor=None)
