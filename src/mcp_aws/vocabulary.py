"""The values a client can pick from, and the one shape they are published in.

A gateway that shows a picker and a `completion/complete` answer typed into a field must
agree about what the legal values are, so both read from here. Producing them is
deliberately cheap: a vocabulary is read whenever someone opens the server, so nothing in
this module may call AWS.

The body shape is the gateway's, not ours — a JSON Schema enum fragment, with two keys
that are not JSON Schema keywords:

    readOne   one of my values, spent here, buys a member
    narrows   one of my values, spent here, buys another listing

See SERVER_AUTHORS.md § Injectable values in the gateway repo.
"""

from __future__ import annotations

from typing import Any, Sequence

from .aws import regions
from .aws.clients import default_region
from .aws.profiles import cached_identity, list_profile_names
from .catalog.loader import Catalog

#: (value, label). The value is what gets sent; the label is what a human reads.
Choice = tuple[str, str]


def enum_body(
    choices: Sequence[Choice],
    *,
    description: str,
    read_one: str | None = None,
    narrows: str | None = None,
) -> dict[str, Any]:
    """A vocabulary as the JSON Schema fragment a client can render a picker from."""
    if read_one and narrows:
        raise ValueError("a listing names readOne or narrows, never both")
    body: dict[str, Any] = {
        "type": "string",
        "enum": [value for value, _ in choices],
        "enumNames": [label for _, label in choices],
        "description": description,
    }
    if read_one:
        body["readOne"] = read_one
    if narrows:
        body["narrows"] = narrows
    return body


def profile_choices() -> list[Choice]:
    """Configured profiles, labelled from whatever identity is already cached.

    A profile is never resolved here. `aws://accounts/{profile}` is where a live STS call
    belongs, because that is a URI someone asked for rather than one they merely looked at.
    """
    out: list[Choice] = []
    for profile in list_profile_names():
        identity = cached_identity(profile)
        if identity:
            who = identity.get("account_alias") or identity.get("account_id") or "resolved"
            where = identity.get("default_region")
            out.append((profile, f"{profile} — {who}" + (f" ({where})" if where else "")))
        else:
            out.append((profile, f"{profile} — not yet resolved"))
    return out


def view_choices(catalog: Catalog, service: str | None = None) -> list[Choice]:
    return [(view.id, view.summary) for view in catalog.search(service=service)]


def service_choices(catalog: Catalog) -> list[Choice]:
    counts: dict[str, int] = {}
    for view in catalog.all():
        counts[view.service] = counts.get(view.service, 0) + 1
    return [
        (service, f"{service} ({counts[service]} view{'s' if counts[service] != 1 else ''})")
        for service in catalog.services
    ]


def _default_region(profile: str) -> str | None:
    """A profile's configured region, from the config file. Still no AWS call."""
    identity = cached_identity(profile)
    if identity and identity.get("default_region"):
        return identity["default_region"]
    try:
        return default_region(profile)
    except Exception:  # noqa: BLE001 - an unknown or unreadable profile simply has none
        return None


def region_choices(profile: str | None = None) -> list[Choice]:
    """Every region of the standard partition, with one profile's own region first.

    Hoisted rather than filtered: querying an account outside its configured default
    region is ordinary, so the other regions stay on offer.
    """
    names = regions.list_regions()
    default = _default_region(profile) if profile else None
    ordered = ([default] if default in names else []) + [n for n in names if n != default]
    return [
        (name, f"{name} — default for {profile}" if name == default else name) for name in ordered
    ]
