"""`completion/complete`: the vocabularies again, offered while someone types.

The handler is registered straight onto the low-level server and there is no public call
path to it, so these drive `complete_argument` — the body it delegates to — and check the
registration separately.
"""

from __future__ import annotations

import anyio
import pytest
from mcp_types import (
    CompletionArgument,
    CompletionContext,
    PromptReference,
    ResourceTemplateReference,
)

from mcp_aws.completions import MAX_COMPLETION_VALUES, complete_argument

VIEW_REF = ResourceTemplateReference(uri="aws://catalog/{view_id}")
REGION_REF = ResourceTemplateReference(uri="aws://regions/{region}")
ACCOUNT_REF = ResourceTemplateReference(uri="aws://accounts/{profile}")
SERVICE_VIEWS_REF = ResourceTemplateReference(uri="aws://services/{service}/views")


def complete(catalog, ref, name, value="", **filled):
    context = CompletionContext(arguments=filled) if filled else None
    return complete_argument(catalog, ref, CompletionArgument(name=name, value=value), context)


def values(catalog, ref, name, value="", **filled):
    result = complete(catalog, ref, name, value, **filled)
    return [] if result is None else result.values


# -------------------------------------------------------------------- registration


def test_the_server_advertises_the_completions_capability(server):
    capabilities = server._lowlevel_server.create_initialization_options().capabilities
    assert capabilities.completions is not None


def test_every_template_variable_has_a_completion(server, catalog):
    """A new template without a vocabulary behind it fails here."""
    from mcp.shared.uri_template import UriTemplate

    async def run():
        return [t.uri_template for t in await server.list_resource_templates()]

    for template in anyio.run(run):
        ref = ResourceTemplateReference(uri=template)
        for variable in UriTemplate.parse(template).variable_names:
            assert values(catalog, ref, variable), f"{template}: {variable} completes nothing"


# ------------------------------------------------------------------------ narrowing


def test_view_id_completion_filters_on_what_was_typed(catalog):
    typed = values(catalog, VIEW_REF, "view_id", "ec2.")
    assert typed and all(v.startswith("ec2.") for v in typed)
    assert set(typed) < set(values(catalog, VIEW_REF, "view_id"))


def test_view_id_completion_narrows_on_a_service_already_filled(catalog):
    # The sibling fields of the same form ride along in context.arguments, so a view_id
    # asked for beside an already-picked service narrows to that service.
    narrowed = values(catalog, VIEW_REF, "view_id", service="eks")
    assert narrowed and all(v.startswith("eks.") for v in narrowed)
    assert set(narrowed) < set(values(catalog, VIEW_REF, "view_id"))


def test_a_stale_service_falls_back_to_every_view(catalog):
    """An argument left over from an earlier pick must not read as 'no views here'."""
    assert values(catalog, VIEW_REF, "view_id", service="nonsense") == values(
        catalog, VIEW_REF, "view_id"
    )


def test_region_completion_hoists_the_chosen_profile_default(catalog, monkeypatch):
    monkeypatch.setattr("mcp_aws.vocabulary._default_region", lambda profile: "eu-west-2")
    hoisted = values(catalog, REGION_REF, "region", profile="prod")
    assert hoisted[0] == "eu-west-2"
    assert len(hoisted) == len(values(catalog, REGION_REF, "region")), (
        "querying a profile outside its default region is legal; the rest stay on offer"
    )


def test_profile_completion_lists_configured_profiles(catalog, fake_profiles):
    assert values(catalog, ACCOUNT_REF, "profile") == fake_profiles


# ------------------------------------------------------------------------ the cap


def test_completions_are_capped_with_total_and_has_more(catalog, monkeypatch):
    many = [f"xx-region-{i}" for i in range(150)]
    monkeypatch.setattr("mcp_aws.vocabulary.regions.list_regions", lambda *a, **k: many)
    result = complete(catalog, REGION_REF, "region")
    assert len(result.values) == MAX_COMPLETION_VALUES
    assert result.has_more is True
    assert result.total == 150, "total is the count before the cap, not after"


# ------------------------------------------------------------------ nothing to say


def test_a_variable_the_template_does_not_name_completes_nothing(catalog):
    """Keying on the argument name alone would let aws://regions suggest a view id."""
    assert complete(catalog, REGION_REF, "view_id") is None


def test_an_unknown_ref_completes_nothing(catalog):
    assert complete(catalog, ResourceTemplateReference(uri="aws://nope/{x}"), "x") is None
    assert complete(catalog, PromptReference(name="not-a-prompt"), "profile") is None


def test_an_open_ended_argument_completes_nothing(catalog):
    """A wrong suggestion for an ARN is worse than no suggestion."""
    assert complete(catalog, VIEW_REF, "cursor") is None


def test_prompt_arguments_complete_from_the_same_vocabulary(catalog, fake_profiles):
    from mcp_aws.prompts import prompt_name

    overview = PromptReference(name=prompt_name("account_overview"))
    assert values(catalog, overview, "profile") == fake_profiles
    assert values(catalog, overview, "region")
    investigate = PromptReference(name=prompt_name("investigate_resource"))
    assert complete(catalog, investigate, "arn") is None


def test_completions_make_no_aws_calls(catalog, fake_profiles, no_aws_calls):
    """They fire on every keystroke."""
    for ref, name in (
        (VIEW_REF, "view_id"),
        (ACCOUNT_REF, "profile"),
        (REGION_REF, "region"),
        (SERVICE_VIEWS_REF, "service"),
    ):
        assert values(catalog, ref, name)
    assert no_aws_calls == []


@pytest.mark.parametrize("value", ["", "EC2.", "nonsense"])
def test_completion_never_raises(catalog, value):
    assert complete(catalog, VIEW_REF, "view_id", value) is not None
