"""The tool layer: discovery, dispatch and the failure messages a model actually sees."""

from __future__ import annotations

import pytest

from mcp_aws import tools
from mcp_aws.errors import McpAwsError


@pytest.fixture
def usable_profile(monkeypatch):
    monkeypatch.setattr(
        tools,
        "resolve_profile",
        lambda profile, refresh=False: {
            "profile": profile,
            "account_id": "123456789012",
            "status": "ok",
        },
    )


def test_catalog_lists_views_and_services(catalog):
    result = tools.tool_catalog(catalog)
    assert result["count"] == len(catalog)
    assert "ec2" in result["services"]
    assert all({"view_id", "summary"} == set(entry) for entry in result["views"])


def test_catalog_filters_by_service(catalog):
    result = tools.tool_catalog(catalog, service="eks")
    assert result["count"] > 0
    assert all(entry["view_id"].startswith("eks.") for entry in result["views"])


def test_catalog_search_matches_summaries(catalog):
    result = tools.tool_catalog(catalog, query="security group")
    assert any("security_groups" in entry["view_id"] for entry in result["views"])


def test_describe_view_exposes_params(catalog):
    result = tools.tool_describe_view(catalog, "ec2.instances.list")
    assert result["aws_call"] == "ec2:describe_instances"
    assert result["params"]["state"]["enum"]
    assert result["params"]["instance_ids"]["type"] == "string[]"


def test_describe_unknown_view_points_at_the_catalog(catalog):
    with pytest.raises(McpAwsError, match="catalog tool"):
        tools.tool_describe_view(catalog, "ec2.nope.list")


def test_query_refuses_an_unusable_profile(catalog, monkeypatch):
    monkeypatch.setattr(
        tools,
        "resolve_profile",
        lambda profile, refresh=False: {
            "status": "unavailable",
            "detail": "credentials expired; run aws sso login --profile dev",
        },
    )
    with pytest.raises(McpAwsError, match="aws sso login"):
        tools.tool_query(catalog, "ec2.instances.list", profile="dev")


def test_read_resource_routes_an_arn_to_its_detail_view(catalog, usable_profile, stub_client):
    client, stubber = stub_client("ec2")
    stubber.add_response(
        "describe_instances",
        {"Reservations": [{"Instances": [{"InstanceId": "i-abc", "State": {"Name": "running"}}]}]},
        expected_params={"InstanceIds": ["i-abc"]},
    )

    result = tools.tool_read_resource(
        catalog, "arn:aws:ec2:us-east-1:123456789012:instance/i-abc", profile="test"
    )

    assert result["view"] == "ec2.instance.detail"
    assert result["items"][0]["id"] == "i-abc"
    assert result["arn"].endswith("i-abc")
    stubber.assert_no_pending_responses()


def test_read_resource_passes_the_whole_arn_where_aws_expects_one(
    catalog, usable_profile, stub_client
):
    arn = "arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/app/web/abc123"
    client, stubber = stub_client("elbv2")
    stubber.add_response(
        "describe_load_balancers",
        {"LoadBalancers": [{"LoadBalancerName": "web", "LoadBalancerArn": arn}]},
        expected_params={"LoadBalancerArns": [arn]},
    )

    result = tools.tool_read_resource(catalog, arn, profile="test")
    assert result["items"][0]["name"] == "web"


def test_read_resource_for_an_uncovered_type_says_so(catalog, usable_profile):
    with pytest.raises(McpAwsError, match="No catalog view describes"):
        tools.tool_read_resource(
            catalog, "arn:aws:kinesis:us-east-1:123456789012:stream/events", profile="test"
        )


def test_read_resource_without_a_matching_profile_asks_for_one(catalog, monkeypatch):
    monkeypatch.setattr(tools, "profile_for_account_id", lambda account_id: None)
    with pytest.raises(McpAwsError, match="Pass profile explicitly"):
        tools.tool_read_resource(catalog, "arn:aws:ec2:us-east-1:999999999999:instance/i-abc")


def test_list_accounts_explains_an_empty_config(monkeypatch):
    monkeypatch.setattr(tools, "list_accounts", lambda refresh=False: [])
    result = tools.tool_list_accounts()
    assert result["accounts"] == []
    assert "~/.aws/config" in result["note"]
