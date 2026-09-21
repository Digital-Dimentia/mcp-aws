"""The five tool implementations.

This module is the entire permanent context cost of the server. Adding AWS coverage
means adding YAML to the catalog, never a sixth tool — the catalog is discovered at
runtime through `aws_catalog`, so a model pays for the one view it needs instead of
carrying several hundred schemas it does not.
"""

from __future__ import annotations

import logging
from typing import Any

from .aws.arn import parse_arn
from .aws.profiles import list_accounts, profile_for_account_id, resolve_profile
from .catalog.engine import run_view
from .catalog.loader import Catalog
from .config import SETTINGS
from .errors import McpAwsError

log = logging.getLogger(__name__)


def tool_list_accounts(refresh: bool = False) -> dict[str, Any]:
    accounts = list_accounts(refresh=refresh)
    if not accounts:
        return {
            "accounts": [],
            "note": "No AWS profiles are configured (or all are filtered out by "
            "MCP_AWS_PROFILES). Check ~/.aws/config.",
        }
    usable = [a for a in accounts if a["status"] == "ok"]
    return {
        "accounts": accounts,
        "count": len(accounts),
        "usable": len(usable),
    }


def tool_catalog(catalog: Catalog, service: str | None = None, query: str | None = None) -> dict[str, Any]:
    views = catalog.search(service=service, query=query)
    return {
        "services": catalog.services,
        "count": len(views),
        "views": [view.listing_entry() for view in views],
        "next_step": "Call the describe-view tool for a view's parameters, then the query tool.",
    }


def tool_describe_view(catalog: Catalog, view_id: str) -> dict[str, Any]:
    try:
        view = catalog.get(view_id)
    except KeyError:
        raise McpAwsError(
            f"No such view '{view_id}'. Use the catalog tool to list available view ids."
        ) from None

    params = {
        name: {
            "type": spec.type,
            "required": spec.required,
            "description": spec.description,
            **({"enum": spec.enum} if spec.enum else {}),
            **({"default": spec.default} if spec.default is not None else {}),
        }
        for name, spec in view.params.items()
    }
    return {
        "view_id": view.id,
        "summary": view.summary,
        "description": view.description,
        "regional": view.regional,
        "paginated": view.paginate or view.expand is not None,
        "aws_call": f"{view.client}:{view.operation}",
        "params": params,
        "returns": view.returns,
        "resolves_arns_for": view.detail_of,
    }


def tool_query(
    catalog: Catalog,
    view_id: str,
    profile: str,
    region: str | None = None,
    params: dict[str, Any] | None = None,
    cursor: str | None = None,
    max_items: int | None = None,
) -> dict[str, Any]:
    try:
        view = catalog.get(view_id)
    except KeyError:
        raise McpAwsError(
            f"No such view '{view_id}'. Use the catalog tool to list available view ids."
        ) from None

    identity = resolve_profile(profile)
    if identity["status"] != "ok":
        raise McpAwsError(identity.get("detail", f"profile '{profile}' is unusable"))

    if max_items is not None:
        max_items = max(1, min(int(max_items), SETTINGS.max_items * 5))

    result = run_view(
        view,
        profile=profile,
        region=region,
        params=params,
        cursor=cursor,
        max_items=max_items,
    )
    result["account_id"] = identity["account_id"]
    return result


def tool_read_resource(catalog: Catalog, arn: str, profile: str | None = None) -> dict[str, Any]:
    parsed = parse_arn(arn)

    view = catalog.detail_view(parsed.service, parsed.resource_type or "")
    if view is None:
        view = catalog.detail_view(parsed.service, "*")
    if view is None:
        raise McpAwsError(
            f"No catalog view describes {parsed.service} "
            f"'{parsed.resource_type or 'resource'}'. "
            "Use the catalog tool to see what this server can read."
        )

    if profile is None:
        if parsed.account_id:
            profile = profile_for_account_id(parsed.account_id)
        if profile is None:
            raise McpAwsError(
                f"Could not match account {parsed.account_id or '(none in ARN)'} to a configured "
                "profile. Pass profile explicitly, or use the list-accounts tool."
            )

    assert view.detail_param is not None  # guaranteed by catalog validation
    identifier = arn if view.detail_from == "arn" else parsed.resource_id
    spec = view.params[view.detail_param]
    value: Any = [identifier] if spec.type == "string[]" else identifier

    result = tool_query(
        catalog,
        view_id=view.id,
        profile=profile,
        region=parsed.region,
        params={view.detail_param: value},
    )
    result["arn"] = arn
    return result
