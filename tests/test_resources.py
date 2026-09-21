"""The gateway contract for resources, asserted as rules rather than as a list of URIs.

An MCP gateway builds a value picker out of a *listing* resource paired with a *template*,
and the pairing is by URI: a template's fixed prefix, up to its first `{` and minus a
trailing separator, must be the listing's URI. Nothing announces a broken pair — the
column simply goes empty — so these tests implement the rule itself and walk whatever the
server happens to publish. A future rename fails here instead of in someone's browser.
"""

from __future__ import annotations

import json

import anyio
import pytest
from mcp.server.mcpserver.exceptions import ResourceNotFoundError
from mcp.shared.uri_template import UriTemplate

#: Templates published with no listing beside them, and why. `aws://query/...` is the one
#: URI here that spends money: a listing at `aws://query` would be read whenever a client
#: opens this server, and reading it would mean running AWS queries nobody asked for.
UNPAIRED_TEMPLATES = {"aws://query/{profile}/{region}/{view_id}"}

VOCABULARY_VARIABLES = {"profile", "region", "view_id", "service"}


def fixed_prefix(uri_template: str) -> str:
    """The gateway's pairing rule, written once."""
    return uri_template.split("{", 1)[0].rstrip("/#?&")


def listings(server) -> list[str]:
    async def run():
        return [str(resource.uri) for resource in await server.list_resources()]

    return anyio.run(run)


def templates(server) -> list[str]:
    async def run():
        return [template.uri_template for template in await server.list_resource_templates()]

    return anyio.run(run)


def read(server, uri: str):
    """One resource's contents. `read_resource` yields dataclasses with `.content`."""

    async def run():
        return list(await server.read_resource(uri))

    contents = anyio.run(run)
    assert len(contents) == 1, f"{uri} returned {len(contents)} contents"
    return contents[0]


def read_json(server, uri: str) -> dict:
    return json.loads(read(server, uri).content)


def enum_listings(server) -> dict[str, dict]:
    return {uri: read_json(server, uri) for uri in listings(server)}


# --------------------------------------------------------------------------- pairing


def test_every_template_pairs_with_a_published_listing(server):
    published = set(listings(server))
    narrowed = {
        body["narrows"] for body in enum_listings(server).values() if "narrows" in body
    }
    for template in templates(server):
        if template in UNPAIRED_TEMPLATES or template in narrowed:
            continue
        prefix = fixed_prefix(template)
        assert prefix in published, (
            f"{template} pairs with nothing: no resource is published at {prefix!r}. "
            f"Published listings: {sorted(published)}"
        )


def test_unpaired_template_allowlist_has_no_stale_entries(server):
    """Half the point of an allowlist is that it rots loudly."""
    assert UNPAIRED_TEMPLATES <= set(templates(server))


def test_no_template_has_a_degenerate_prefix(server):
    """`aws://{profile}/...` would pair with nothing and shadow every other template."""
    for template in templates(server):
        assert fixed_prefix(template) not in ("aws:", "aws://", ""), template


# ----------------------------------------------------------------------- body shape


def test_every_listing_is_json_text(server):
    for uri in listings(server):
        content = read(server, uri)
        assert isinstance(content.content, str), f"{uri} is not text"
        assert content.mime_type == "application/json", uri
        json.loads(content.content)


def test_every_listing_is_a_non_empty_enum_fragment(server):
    for uri, body in enum_listings(server).items():
        assert body["type"] == "string", uri
        assert isinstance(body["enum"], list) and body["enum"], f"{uri} has an empty enum"
        assert all(isinstance(value, str) for value in body["enum"]), uri
        assert len(body["enumNames"]) == len(body["enum"]), uri
        assert body["description"], uri


def test_every_listing_names_exactly_one_of_read_one_or_narrows(server):
    for uri, body in enum_listings(server).items():
        named = [key for key in ("readOne", "narrows") if key in body]
        assert named == [named[0]] and len(named) == 1, (
            f"{uri} names {named or 'neither readOne nor narrows'}; a listing has to say "
            f"where one of its values is spent"
        )


def test_every_read_one_and_narrows_target_is_published(server):
    published = set(templates(server))
    for uri, body in enum_listings(server).items():
        target = body.get("readOne") or body.get("narrows")
        assert target in published, f"{uri} points at {target}, which is not published"


def test_narrows_target_reads_for_every_parent_value(server):
    """The cascade invariant: a narrowed listing is only ever read at a URI this server
    produced, so every value the parent publishes must expand into something real."""
    for uri, body in enum_listings(server).items():
        if "narrows" not in body:
            continue
        template = UriTemplate.parse(body["narrows"])
        (variable,) = template.variable_names
        for value in body["enum"]:
            child = read_json(server, template.expand({variable: value}))
            assert child["enum"], f"{uri} -> {value} narrowed to an empty listing"


# ------------------------------------------------------------------------- no calls


def test_listings_make_no_aws_calls(server, no_aws_calls):
    """A listing is read on open, on Reread and on every list_changed."""
    for uri, body in enum_listings(server).items():
        assert body["enum"], f"{uri} degraded to an empty enum when AWS was unreachable"
    assert no_aws_calls == [], f"reading listings made {len(no_aws_calls)} AWS call(s)"


def test_regions_listing_is_offline(server, no_aws_calls):
    """Pinned so a future switch to ec2:DescribeRegions fails here, not in production."""
    body = read_json(server, "aws://regions")
    assert "us-east-1" in body["enum"] and len(body["enum"]) > 20
    assert no_aws_calls == []


def test_accounts_listing_names_configured_profiles_without_resolving_them(
    server, fake_profiles, no_aws_calls
):
    body = read_json(server, "aws://accounts")
    assert body["enum"] == fake_profiles
    assert no_aws_calls == []


# ------------------------------------------------------------------ variable naming


def test_template_variables_fill_a_real_tool_argument(server, tool_surface):
    """The gateway matches a value group to a field by name, so the two must not drift."""
    tool_arguments = {
        name for tool in tool_surface for name in tool["input_schema"]["properties"]
    }
    for template in templates(server):
        for variable in UriTemplate.parse(template).variable_names:
            assert variable in tool_arguments, (
                f"{template} names {{{variable}}}, which fills no tool argument. "
                f"Tool arguments: {sorted(tool_arguments)}"
            )


def test_the_four_vocabulary_variables_are_all_published(server):
    published = {
        variable
        for template in templates(server)
        for variable in UriTemplate.parse(template).variable_names
    }
    assert VOCABULARY_VARIABLES <= published


# -------------------------------------------------------------------------- errors


@pytest.mark.parametrize(
    "uri",
    [
        "aws://catalog/nope.nope",
        "aws://regions/nope-1",
        "aws://accounts/nope",
        "aws://services/nope/views",
        "aws://nope",
    ],
)
def test_a_uri_this_server_never_published_is_an_error(server, uri):
    """Not an empty body: 'nothing here' is a different and usually wrong fact."""
    with pytest.raises(ResourceNotFoundError):
        anyio.run(lambda: server.read_resource(uri))


def test_not_found_messages_point_at_the_listing(server):
    with pytest.raises(ResourceNotFoundError, match="aws://catalog"):
        anyio.run(lambda: server.read_resource("aws://catalog/nope.nope"))


# ------------------------------------------------------------------------ contents


def test_catalog_listing_enumerates_every_view(server, catalog):
    body = read_json(server, "aws://catalog")
    assert body["enum"] == [view.id for view in catalog.all()]
    assert body["enumNames"] == [view.summary for view in catalog.all()]


def test_view_member_describes_the_aws_call(server, catalog):
    view = catalog.all()[0]
    body = read_json(server, f"aws://catalog/{view.id}")
    assert body["view_id"] == view.id
    assert body["aws_call"] == f"{view.client}:{view.operation}"
