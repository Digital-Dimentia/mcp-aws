"""`completion/complete`: the same vocabularies, offered while someone is typing.

A picker is how a person browses a set; a completion is how a field gets filled without
leaving the keyboard. Both answer from `vocabulary.py`, so a suggestion and a chip can
never disagree.

Two rules here. The handler never calls AWS — it fires on every keystroke. And it never
raises: the SDK turns any unexpected exception into an internal error, where a caller
absorbs an empty answer without noticing.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp_types import (
    Completion,
    CompletionArgument,
    CompletionContext,
    PromptReference,
    ResourceTemplateReference,
)

from .catalog.loader import Catalog
from .prompts import PROMPT_ARGUMENTS, prompt_name
from .vocabulary import Choice, profile_choices, region_choices, service_choices, view_choices

log = logging.getLogger(__name__)

#: The protocol's cap on one completion result. Past it, values are reported, not sent.
MAX_COMPLETION_VALUES = 100


def _choices_for(catalog: Catalog, name: str, filled: dict[str, str]) -> list[Choice] | None:
    """The vocabulary for one argument name, or None when there is no sensible one.

    `arn`, `cursor`, `params`, `max_items` and the catalog tool's free-text `query` are
    open-ended on purpose: a wrong suggestion in one of those is worse than none.
    """
    if name == "view_id":
        service = filled.get("service")
        # An unknown service falls back to everything: a field left over from an earlier
        # pick should not read as "this service has no views".
        if service not in catalog.services:
            service = None
        return view_choices(catalog, service=service)
    if name == "profile":
        return profile_choices()
    if name == "region":
        return region_choices(profile=filled.get("profile") or None)
    if name == "service":
        return service_choices(catalog)
    return None


def _names_on(ref: Any) -> frozenset[str]:
    """Which arguments this ref actually has, so a vocabulary is only offered where it fits.

    Keying on the argument name alone would let `aws://regions/{region}` be asked to
    suggest a view id.
    """
    if isinstance(ref, ResourceTemplateReference):
        uri = ref.uri or ""
        return frozenset(name for name in ("view_id", "profile", "region", "service")
                         if "{%s}" % name in uri)
    if isinstance(ref, PromptReference):
        for stem, arguments in PROMPT_ARGUMENTS.items():
            if ref.name == prompt_name(stem):
                return frozenset(arguments)
    return frozenset()


def complete_argument(
    catalog: Catalog,
    ref: Any,
    argument: CompletionArgument,
    context: CompletionContext | None,
) -> Completion | None:
    """The completion handler's body, separated so it can be called without a session."""
    try:
        if argument.name not in _names_on(ref):
            return None
        choices = _choices_for(catalog, argument.name, (context.arguments if context else None) or {})
        if choices is None:
            return None

        typed = (argument.value or "").lower()
        hits = [
            value
            for value, label in choices
            if not typed or value.lower().startswith(typed) or label.lower().startswith(typed)
        ]
        return Completion(
            values=hits[:MAX_COMPLETION_VALUES],
            total=len(hits),
            has_more=len(hits) > MAX_COMPLETION_VALUES,
        )
    except Exception:  # noqa: BLE001 - a broken suggestion must not break the request
        log.exception("completion for %r failed", getattr(argument, "name", argument))
        return None


def register_completions(server: Any, catalog: Catalog) -> None:
    """Registering the handler is also what declares the `completions` capability."""

    @server.completion()
    async def complete(
        ref: Any, argument: CompletionArgument, context: CompletionContext | None
    ) -> Completion | None:
        return complete_argument(catalog, ref, argument, context)
