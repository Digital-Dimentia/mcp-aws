"""Executes a catalog view against AWS.

The engine is the only place that talks to boto3 on behalf of a query, and it can only
do what a validated view describes: build the request from declared params, page
through the response, project each page, optionally fan out to a per-item detail call,
and hand the result to the budget for capping.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import jmespath

from ..aws.clients import TTLCache, client_for, default_region
from ..budget import Cursor, Origin, trim
from ..config import SETTINGS
from ..errors import McpAwsError, describe_failure
from .models import ExpandSpec, ParamSpec, View

log = logging.getLogger(__name__)

_result_cache = TTLCache()

_COERCE = {
    "string": str,
    "integer": int,
    "boolean": lambda v: v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes"),
}


def _coerce(name: str, spec: ParamSpec, value: Any) -> Any:
    if spec.type == "string[]":
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        if isinstance(value, list):
            return [str(item) for item in value]
        raise McpAwsError(f"param '{name}' expects a list of strings, got {type(value).__name__}")
    try:
        return _COERCE[spec.type](value)
    except (TypeError, ValueError) as exc:
        raise McpAwsError(f"param '{name}' expects {spec.type}: {exc}") from exc


def _assign(request: dict[str, Any], path: str, value: Any) -> None:
    """Write a value into the request at a dotted path, creating intermediate dicts."""
    parts = path.split(".")
    target = request
    for part in parts[:-1]:
        target = target.setdefault(part, {})
    target[parts[-1]] = value


def build_request(view: View, params: dict[str, Any] | None) -> dict[str, Any]:
    """Turn caller params into AWS request kwargs. Unknown params are rejected loudly —
    silently ignoring one produces an answer to a question nobody asked."""
    supplied = dict(params or {})
    unknown = sorted(set(supplied) - set(view.params))
    if unknown:
        raise McpAwsError(
            f"unknown param(s) for {view.id}: {', '.join(unknown)}. "
            f"Declared params: {', '.join(sorted(view.params)) or 'none'}"
        )

    request: dict[str, Any] = dict(view.static_params)
    filters: dict[str, list[Any]] = {}
    # Dynamic-key filters are merged by (request member, key) so a key param and a
    # values param declared separately collapse into one AWS filter entry.
    dynamic: dict[tuple[str, str], dict[str, Any]] = {}

    for name, spec in view.params.items():
        value = supplied.get(name, spec.default)
        if value is None:
            if spec.required:
                raise McpAwsError(f"param '{name}' is required for {view.id}")
            continue

        value = _coerce(name, spec, value)

        if spec.enum:
            candidates = value if isinstance(value, list) else [value]
            invalid = [str(c) for c in candidates if str(c) not in spec.enum]
            if invalid:
                raise McpAwsError(
                    f"param '{name}' must be one of {', '.join(spec.enum)}; got {', '.join(invalid)}"
                )

        dynamic_source = spec.dynamic_filter_key
        if dynamic_source is not None:
            key_field = "Key" if spec.filter_style == "key_values" else "Name"
            if dynamic_source == "self":
                filter_key, values = str(value), []
            else:
                raw_key = supplied.get(dynamic_source, view.params[dynamic_source].default)
                if raw_key is None:
                    raise McpAwsError(
                        f"param '{name}' also requires '{dynamic_source}' to be set"
                    )
                filter_key = str(raw_key)
                values = [str(v) for v in (value if isinstance(value, list) else [value])]
            entry = dynamic.setdefault(
                (spec.root_key, filter_key), {key_field: filter_key, "Values": []}
            )
            entry["Values"].extend(values)
            continue

        filter_name = spec.filter_name
        if filter_name is not None:
            values = value if isinstance(value, list) else [value]
            key_field = "Key" if spec.filter_style == "key_values" else "Name"
            filters.setdefault(spec.root_key, []).append(
                {key_field: filter_name, "Values": [str(v) for v in values]}
            )
        else:
            _assign(request, spec.maps_to, value)

    for root_key, entries in filters.items():
        request.setdefault(root_key, []).extend(entries)

    for (root_key, _), entry in dynamic.items():
        request.setdefault(root_key, []).append(entry)

    return request


def _project(expression: str | None, payload: Any) -> list[Any]:
    if expression is None:
        result = payload
    else:
        result = jmespath.search(expression, payload)
    if result is None:
        return []
    if isinstance(result, list):
        return [item for item in result if item is not None]
    return [result]


def _resolve_region(view: View, profile: str, region: str | None) -> str | None:
    if not view.regional:
        return view.global_region
    return region or default_region(profile)


def _expand_items(
    expand: ExpandSpec,
    view: View,
    profile: str,
    region: str | None,
    parent_items: list[Any],
    parent_request: dict[str, Any],
) -> list[Any]:
    """Run the per-item follow-up call. Bounded fan-out, failures reported per item."""
    client_name = expand.client or view.client
    client = client_for(profile, client_name, region)
    limited = parent_items[: expand.max_items]
    if len(parent_items) > expand.max_items:
        log.info(
            "%s: expanding only the first %d of %d items",
            view.id,
            expand.max_items,
            len(parent_items),
        )

    def fetch(item: Any) -> Any:
        arg_value = jmespath.search(expand.arg_from, item) if expand.arg_from != "@" else item
        inherited = {key: parent_request[key] for key in expand.inherit if key in parent_request}
        request = {**expand.static_params, **inherited, expand.arg: arg_value}
        try:
            response = getattr(client, expand.operation)(**request)
        except Exception as exc:  # noqa: BLE001 - one bad item must not fail the view
            return {
                str(expand.arg): arg_value,
                "error": describe_failure(exc, profile=profile, region=region),
            }
        projected = _project(expand.project, response)
        return projected[0] if len(projected) == 1 else projected

    workers = max(1, min(SETTINGS.expand_max_concurrency, len(limited) or 1))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(fetch, limited))


def _next_page_token(pages: Any, page: dict[str, Any]) -> str | None:
    """The StartingToken that would begin the page *after* this one.

    botocore only fills in `PageIterator.resume_token` when it truncates a response
    itself (via MaxItems), and its MaxItems counts result-key entries rather than the
    items our projection produces — DescribeInstances pages by Reservation, not by
    Instance. So the token is derived from the page directly, using the same encoder
    botocore uses for its own resume tokens.
    """
    try:
        tokens = pages._get_next_token(page)
        if not tokens:
            return None
        return pages._token_encoder.encode(tokens)
    except Exception:  # noqa: BLE001 - botocore internals moved; fall back to its own
        log.debug("falling back to PageIterator.resume_token", exc_info=True)
        return pages.resume_token


def _collect(
    view: View,
    client: Any,
    request: dict[str, Any],
    cursor: Cursor,
    limit: int,
) -> tuple[list[Any], list[Origin], str | None]:
    """Fetch and project up to `limit` items, recording each item's page of origin."""
    items: list[Any] = []
    origins: list[Origin] = []

    if not view.paginate:
        response = getattr(client, view.operation)(**request)
        page_items = _project(view.project, response)
        for index, item in enumerate(page_items):
            if index < cursor.skip:
                continue
            items.append(item)
            origins.append(Origin(page_token=None, index=index))
        return items, origins, None

    paginator = client.get_paginator(view.operation)
    pagination_config: dict[str, Any] = {}
    if cursor.page_token:
        pagination_config["StartingToken"] = cursor.page_token
    pages = paginator.paginate(**request, PaginationConfig=pagination_config)

    token_for_page = cursor.page_token
    skip = cursor.skip

    for page in pages:
        page_items = _project(view.project, page)
        for index, item in enumerate(page_items):
            if index < skip:
                continue
            items.append(item)
            origins.append(Origin(page_token=token_for_page, index=index))
        skip = 0
        token_for_page = _next_page_token(pages, page)
        if len(items) >= limit:
            # Stop at a page boundary; anything beyond `limit` is cut by trim(), which
            # derives the exact resume point from the origins above.
            return items, origins, token_for_page if len(items) == limit else None

    return items, origins, None


def run_view(
    view: View,
    *,
    profile: str,
    region: str | None = None,
    params: dict[str, Any] | None = None,
    cursor: str | None = None,
    max_items: int | None = None,
) -> dict[str, Any]:
    """Execute one view and return the uniform response envelope."""
    limit = max_items or SETTINGS.max_items
    resolved_region = _resolve_region(view, profile, region)
    request = build_request(view, params)
    decoded = Cursor.decode(cursor)

    cache_key = (
        view.id,
        profile,
        resolved_region,
        repr(sorted((params or {}).items())),
        cursor,
        limit,
    )
    cached = _result_cache.get(cache_key)
    if cached is not None:
        return {**cached, "cached": True}

    if view.regional and resolved_region is None:
        raise McpAwsError(
            f"{view.id} is regional and profile '{profile}' has no default region; "
            "pass region explicitly."
        )

    client = client_for(profile, view.client, resolved_region)

    try:
        items, origins, more_token = _collect(view, client, request, decoded, limit)
        if view.expand is not None:
            items = _expand_items(
                view.expand, view, profile, resolved_region, items, request
            )
            origins = [Origin(page_token=None, index=i) for i in range(len(items))]
            more_token = None
    except McpAwsError:
        raise
    except Exception as exc:  # noqa: BLE001 - surfaced as an actionable message
        raise McpAwsError(describe_failure(exc, profile=profile, region=resolved_region)) from exc

    trimmed = trim(
        items,
        origins,
        max_items=limit,
        max_chars=SETTINGS.max_chars,
        more_pages_token=more_token,
    )

    envelope = {
        "view": view.id,
        "profile": profile,
        "region": resolved_region,
        "count": len(trimmed.items),
        "truncated": trimmed.truncated,
        "next_cursor": trimmed.next_cursor,
        "items": trimmed.items,
    }
    _result_cache.put(cache_key, envelope)
    return envelope


def reset_cache() -> None:
    _result_cache.clear()
