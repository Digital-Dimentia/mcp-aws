"""The whole point of the design: a fixed, small tool surface.

If these fail, the server has started paying context rent per AWS capability again.
"""

from __future__ import annotations

import json

import anyio

from mcp_aws.server import build_server, dump_tool_surface

EXPECTED_TOOLS = {
    "aws_list_accounts",
    "aws_catalog",
    "aws_describe_view",
    "aws_query",
    "aws_read_resource",
}

# Roughly 4 characters per token; the surface must stay well under 2k tokens no matter
# how far the catalog grows.
MAX_SURFACE_CHARS = 8_000


def _tools(server):
    async def run():
        return await server.list_tools()

    return anyio.run(run)


def test_exactly_five_tools_regardless_of_catalog_size(catalog):
    server = build_server(catalog)
    assert {tool.name for tool in _tools(server)} == EXPECTED_TOOLS


def test_tool_surface_stays_within_the_context_budget():
    surface = dump_tool_surface()
    assert len(surface) < MAX_SURFACE_CHARS, (
        f"tool surface grew to {len(surface)} chars (~{len(surface) // 4} tokens)"
    )


def test_tools_publish_real_schemas_not_varargs():
    """A wrapper that loses the signature would publish (*args, **kwargs) and silently
    make every tool unusable."""
    surface = json.loads(dump_tool_surface())
    by_name = {tool["name"]: tool for tool in surface}

    query_schema = by_name["aws_query"]["input_schema"]
    assert set(query_schema["required"]) == {"view_id", "profile"}
    assert set(query_schema["properties"]) == {
        "view_id",
        "profile",
        "region",
        "params",
        "cursor",
        "max_items",
    }
    for tool in surface:
        assert "kwargs" not in tool["input_schema"]["properties"], tool["name"]


def test_every_tool_is_annotated_read_only():
    for tool in json.loads(dump_tool_surface()):
        assert tool["annotations"]["read_only_hint"] is True, tool["name"]
        assert tool["annotations"]["destructive_hint"] is False, tool["name"]


def test_tool_prefix_is_configurable(monkeypatch, catalog):
    """The gateway may need to namespace this server alongside others."""
    from mcp_aws import server as server_module
    from mcp_aws.config import Settings

    monkeypatch.setattr(server_module, "SETTINGS", Settings(tool_prefix="corp_aws_"))
    names = {tool.name for tool in _tools(build_server(catalog))}
    assert names == {f"corp_aws_{name[4:]}" for name in EXPECTED_TOOLS}
