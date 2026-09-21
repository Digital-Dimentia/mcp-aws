"""A thin MCP resource layer mirroring the catalog.

Tools remain the path a model actually uses; resources exist so a human can browse or
@-mention AWS data in clients that support them, and so a client can preload "what can
this server do" without spending a tool call.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from .aws.profiles import list_accounts
from .catalog.loader import Catalog
from .errors import McpAwsError


def _dump(payload: Any) -> str:
    return json.dumps(payload, indent=2, default=str)


def register_resources(server: Any, catalog: Catalog, runner: Callable[..., dict]) -> None:
    @server.resource(
        "aws://catalog",
        name="AWS catalog",
        description="Every read-only view this server offers, with parameters.",
        mime_type="application/json",
    )
    def catalog_resource() -> str:
        return _dump(
            {
                "services": catalog.services,
                "views": [
                    {
                        "view_id": view.id,
                        "summary": view.summary,
                        "regional": view.regional,
                        "params": sorted(view.params),
                    }
                    for view in catalog.all()
                ],
            }
        )

    @server.resource(
        "aws://accounts",
        name="AWS accounts",
        description="Configured profiles resolved to account id, alias and status.",
        mime_type="application/json",
    )
    def accounts_resource() -> str:
        return _dump(list_accounts())

    @server.resource(
        "aws://{profile}/{region}/{view_id}",
        name="AWS view result",
        description="Run a catalog view against one profile and region.",
        mime_type="application/json",
    )
    def view_resource(profile: str, region: str, view_id: str) -> str:
        try:
            view = catalog.get(view_id)
        except KeyError:
            raise McpAwsError(f"No such view '{view_id}'; see aws://catalog") from None
        # 'default' lets a URI stay readable when the profile's own region is wanted.
        resolved = None if region in ("default", "-") else region
        return _dump(runner(view, profile=profile, region=resolved))
