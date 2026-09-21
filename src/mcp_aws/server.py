"""MCPServer wiring: five tools, a thin resource layer, stdio transport."""

from __future__ import annotations

import functools
import json
import logging
import sys
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from . import __version__, logging_setup
from .catalog.engine import run_view
from .catalog.loader import Catalog, CatalogError, load_catalog
from .completions import register_completions
from .config import SETTINGS
from .errors import McpAwsError
from .prompts import register_prompts
from .resources import register_resources
from . import tools

log = logging.getLogger(__name__)

INSTRUCTIONS = """\
Read-only access to AWS accounts. This server deliberately exposes a small, fixed set of
tools over a large catalog of named "views" — each view is one read-only AWS question.

Normal flow:
  1. {p}list_accounts        -> which profiles (accounts) are configured and usable
  2. {p}catalog              -> view ids + summaries (filter by service or search terms)
  3. {p}describe_view        -> a view's parameters, before calling it the first time
  4. {p}query                -> run a view against a profile and region
Use {p}read_resource when you already hold an ARN.

Results are capped; when `truncated` is true, pass `next_cursor` back to continue.
Nothing here can modify AWS.
"""

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)


def _guarded(fn: Any) -> Any:
    """Turn expected failures into readable tool output instead of protocol errors.

    functools.wraps matters here beyond cosmetics: the server derives each tool's input
    schema from the wrapped function's signature, so losing it would publish every tool
    as (*args, **kwargs).
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except McpAwsError as exc:
            return {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - never take the server down on one call
            log.exception("unhandled error in %s", getattr(fn, "__name__", fn))
            return {"error": f"{type(exc).__name__}: {exc}"}

    return wrapper


def build_server(catalog: Catalog | None = None) -> MCPServer:
    catalog = catalog if catalog is not None else load_catalog()
    prefix = SETTINGS.tool_prefix
    server = MCPServer(
        name="mcp-aws",
        version=__version__,
        instructions=INSTRUCTIONS.format(p=prefix),
    )

    def list_accounts(refresh: bool = False) -> dict[str, Any]:
        """List the AWS accounts (named profiles) this server can read, with account id,
        alias, default region and whether the credentials currently work.

        Args:
            refresh: Re-resolve identities instead of using the cached ones.
        """
        return tools.tool_list_accounts(refresh=refresh)

    def catalog_tool(service: str | None = None, query: str | None = None) -> dict[str, Any]:
        """List the read-only views this server offers, as view ids with one-line
        summaries. Start here to find out what can be asked about AWS.

        Args:
            service: Restrict to one service prefix, e.g. 'ec2', 'eks', 'account'.
            query: Space-separated terms matched against id, summary and description.
        """
        return tools.tool_catalog(catalog, service=service, query=query)

    def describe_view(view_id: str) -> dict[str, Any]:
        """Show one view's parameters, output shape and the AWS call behind it.

        Args:
            view_id: A view id from the catalog tool, e.g. 'ec2.instances.list'.
        """
        return tools.tool_describe_view(catalog, view_id)

    def query(
        view_id: str,
        profile: str,
        region: str | None = None,
        params: dict[str, Any] | None = None,
        cursor: str | None = None,
        max_items: int | None = None,
    ) -> dict[str, Any]:
        """Run a catalog view against one account and region.

        Args:
            view_id: A view id from the catalog tool.
            profile: The AWS profile (account) to read, from the list-accounts tool.
            region: AWS region; defaults to the profile's configured region.
            params: View-specific parameters; see the describe-view tool.
            cursor: `next_cursor` from a previous truncated result.
            max_items: Cap on returned items for this call.
        """
        return tools.tool_query(
            catalog,
            view_id=view_id,
            profile=profile,
            region=region,
            params=params,
            cursor=cursor,
            max_items=max_items,
        )

    def read_resource(arn: str, profile: str | None = None) -> dict[str, Any]:
        """Describe a single AWS resource given its ARN, routing to the view that covers
        that resource type.

        Args:
            arn: Full ARN, e.g. 'arn:aws:eks:us-east-1:123456789012:cluster/prod'.
            profile: Override the profile; by default it is matched from the ARN account.
        """
        return tools.tool_read_resource(catalog, arn, profile=profile)

    for name, fn in (
        ("list_accounts", list_accounts),
        ("catalog", catalog_tool),
        ("describe_view", describe_view),
        ("query", query),
        ("read_resource", read_resource),
    ):
        server.add_tool(_guarded(fn), name=f"{prefix}{name}", annotations=READ_ONLY)

    # None of the three touches the tool surface: resources, prompts and completions are
    # fetched on demand rather than resent every turn, which is what makes coverage here
    # free where a sixth tool would not be.
    register_resources(server, catalog, run_view)
    register_prompts(server)
    register_completions(server, catalog)
    log.info("mcp-aws %s ready: 5 tools, %d catalog views", __version__, len(catalog))
    return server


def main() -> None:
    logging_setup.configure()
    try:
        server = build_server()
    except CatalogError as exc:
        # The catalog is broken; starting up would only produce confusing failures later.
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc
    server.run(transport="stdio")


def dump_tool_surface() -> str:
    """Serialize the registered tool schemas. Used by the token-budget test."""
    import anyio

    server = build_server()

    async def _collect() -> list[Any]:
        return await server.list_tools()

    listed = anyio.run(_collect)
    return json.dumps([tool.model_dump(mode="json", exclude_none=True) for tool in listed])


if __name__ == "__main__":
    main()
