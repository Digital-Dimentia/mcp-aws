"""Engine behaviour against stubbed AWS responses — no network, no credentials."""

from __future__ import annotations

import pytest

from botocore.paginate import TokenEncoder

from mcp_aws.budget import Cursor
from mcp_aws.catalog.engine import build_request, run_view
from mcp_aws.errors import McpAwsError


def _page_token(value: str) -> str:
    """The StartingToken botocore would hand back for a page whose NextToken is value."""
    return TokenEncoder().encode({"NextToken": value})


def _instances(ids, token=None):
    page = {
        "Reservations": [
            {
                "Instances": [
                    {
                        "InstanceId": iid,
                        "InstanceType": "t3.micro",
                        "State": {"Name": "running"},
                        "Placement": {"AvailabilityZone": "us-east-1a"},
                        "VpcId": "vpc-1",
                        "SubnetId": "subnet-1",
                        "PrivateIpAddress": "10.0.0.1",
                        "Tags": [{"Key": "Name", "Value": f"host-{iid}"}],
                    }
                    for iid in ids
                ]
            }
        ]
    }
    if token:
        page["NextToken"] = token
    return page


# --- request building ---------------------------------------------------------------


def test_filters_and_lists_map_into_the_request(catalog):
    view = catalog.get("ec2.instances.list")
    request = build_request(view, {"state": "running", "vpc_id": "vpc-1", "instance_ids": "i-1,i-2"})
    assert request["InstanceIds"] == ["i-1", "i-2"]
    assert {"Name": "instance-state-name", "Values": ["running"]} in request["Filters"]
    assert {"Name": "vpc-id", "Values": ["vpc-1"]} in request["Filters"]


def test_unknown_param_is_rejected(catalog):
    view = catalog.get("ec2.instances.list")
    with pytest.raises(McpAwsError, match="unknown param"):
        build_request(view, {"vpcid": "vpc-1"})


def test_enum_is_enforced(catalog):
    view = catalog.get("ec2.instances.list")
    with pytest.raises(McpAwsError, match="must be one of"):
        build_request(view, {"state": "on-fire"})


def test_required_param_is_enforced(catalog):
    view = catalog.get("eks.cluster.detail")
    with pytest.raises(McpAwsError, match="required"):
        build_request(view, {})


def test_dynamic_filter_key_merges_key_and_values(catalog):
    view = catalog.get("resources.by_tag")
    request = build_request(view, {"tag_key": "Team", "tag_values": ["platform", "data"]})
    assert request["TagFilters"] == [{"Key": "Team", "Values": ["platform", "data"]}]


def test_dynamic_filter_key_alone_matches_any_value(catalog):
    view = catalog.get("resources.by_tag")
    request = build_request(view, {"tag_key": "Team"})
    assert request["TagFilters"] == [{"Key": "Team", "Values": []}]


def test_dependent_param_without_its_key_is_rejected(catalog):
    view = catalog.get("resources.by_tag")
    with pytest.raises(McpAwsError, match="requires 'tag_key'"):
        build_request(view, {"tag_values": ["platform"]})


# --- execution ----------------------------------------------------------------------


def test_projection_shapes_the_items(catalog, stub_client):
    client, stubber = stub_client("ec2")
    stubber.add_response("describe_instances", _instances(["i-1"]))

    result = run_view(catalog.get("ec2.instances.list"), profile="test", region="us-east-1")

    assert result["count"] == 1
    assert result["items"][0] == {
        "id": "i-1",
        "name": "host-i-1",
        "type": "t3.micro",
        "state": "running",
        "az": "us-east-1a",
        "vpc": "vpc-1",
        "subnet": "subnet-1",
        "private_ip": "10.0.0.1",
        "public_ip": None,
        "launched": None,
    }
    assert result["truncated"] is False
    stubber.assert_no_pending_responses()


def test_pagination_stops_at_the_limit_and_returns_a_cursor(catalog, stub_client):
    client, stubber = stub_client("ec2")
    stubber.add_response("describe_instances", _instances(["i-1", "i-2"], token="page-2"))
    stubber.add_response("describe_instances", _instances(["i-3", "i-4"], token="page-3"))

    result = run_view(
        catalog.get("ec2.instances.list"), profile="test", region="us-east-1", max_items=3
    )

    assert [item["id"] for item in result["items"]] == ["i-1", "i-2", "i-3"]
    assert result["truncated"] is True
    # i-4 was the first item dropped: index 1 of the page that started at page-2.
    assert Cursor.decode(result["next_cursor"]) == Cursor(page_token=_page_token("page-2"), skip=1)


def test_a_cursor_resumes_without_repeating_items(catalog, stub_client):
    client, stubber = stub_client("ec2")
    stubber.add_response("describe_instances", _instances(["i-3", "i-4"]))

    cursor = Cursor(page_token=_page_token("page-2"), skip=1).encode()
    result = run_view(
        catalog.get("ec2.instances.list"), profile="test", region="us-east-1", cursor=cursor
    )

    assert [item["id"] for item in result["items"]] == ["i-4"]
    assert result["truncated"] is False


def test_empty_result_is_an_empty_list_not_an_error(catalog, stub_client):
    client, stubber = stub_client("ec2")
    stubber.add_response("describe_instances", {"Reservations": []})

    result = run_view(catalog.get("ec2.instances.list"), profile="test", region="us-east-1")
    assert result["items"] == []
    assert result["count"] == 0


def test_aws_errors_become_actionable_messages(catalog, stub_client):
    client, stubber = stub_client("ec2")
    stubber.add_client_error(
        "describe_instances", service_error_code="UnauthorizedOperation", http_status_code=403
    )

    with pytest.raises(McpAwsError) as excinfo:
        run_view(catalog.get("ec2.instances.list"), profile="prod", region="us-east-1")

    message = str(excinfo.value)
    assert "UnauthorizedOperation" in message
    assert "prod" in message and "us-east-1" in message
    assert "lacks the required read permission" in message


def test_expand_fans_out_to_the_detail_call(catalog, stub_client):
    client, stubber = stub_client("eks")
    stubber.add_response("list_clusters", {"clusters": ["prod", "staging"]})
    for name in ("prod", "staging"):
        stubber.add_response(
            "describe_cluster",
            {
                "cluster": {
                    "name": name,
                    "version": "1.30",
                    "status": "ACTIVE",
                    "endpoint": f"https://{name}.eks.amazonaws.com",
                    "platformVersion": "eks.5",
                    "resourcesVpcConfig": {"vpcId": "vpc-1", "subnetIds": ["subnet-1"]},
                    "logging": {"clusterLogging": [{"types": ["api"], "enabled": True}]},
                }
            },
            expected_params={"name": name},
        )

    result = run_view(catalog.get("eks.clusters.list"), profile="test", region="us-east-1")

    assert sorted(item["name"] for item in result["items"]) == ["prod", "staging"]
    assert result["items"][0]["version"] == "1.30"
    assert result["items"][0]["logging"] == ["api"]
    stubber.assert_no_pending_responses()


def test_expand_inherits_the_parent_key(catalog, stub_client):
    client, stubber = stub_client("eks")
    stubber.add_response(
        "list_nodegroups", {"nodegroups": ["ng-1"]}, expected_params={"clusterName": "prod"}
    )
    stubber.add_response(
        "describe_nodegroup",
        {
            "nodegroup": {
                "nodegroupName": "ng-1",
                "status": "ACTIVE",
                "capacityType": "ON_DEMAND",
                "instanceTypes": ["m6i.large"],
                "scalingConfig": {"desiredSize": 3, "minSize": 1, "maxSize": 6},
            }
        },
        # The child call needs both the parent's cluster name and the item's own name.
        expected_params={"clusterName": "prod", "nodegroupName": "ng-1"},
    )

    result = run_view(
        catalog.get("eks.nodegroups.list"),
        profile="test",
        region="us-east-1",
        params={"cluster": "prod"},
    )

    assert result["items"][0]["name"] == "ng-1"
    assert result["items"][0]["desired"] == 3
    stubber.assert_no_pending_responses()


def test_one_failing_item_does_not_fail_the_whole_expand(catalog, stub_client):
    client, stubber = stub_client("eks")
    stubber.add_response("list_clusters", {"clusters": ["good", "bad"]})
    stubber.add_response(
        "describe_cluster",
        {"cluster": {"name": "good", "status": "ACTIVE", "resourcesVpcConfig": {}, "logging": {}}},
        expected_params={"name": "good"},
    )
    stubber.add_client_error(
        "describe_cluster", service_error_code="AccessDeniedException", http_status_code=403
    )

    result = run_view(catalog.get("eks.clusters.list"), profile="test", region="us-east-1")

    assert result["count"] == 2
    errors = [item for item in result["items"] if "error" in item]
    assert len(errors) == 1
    assert "AccessDeniedException" in errors[0]["error"]
