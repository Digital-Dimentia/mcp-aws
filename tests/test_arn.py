from __future__ import annotations

import pytest

from mcp_aws.aws.arn import parse_arn
from mcp_aws.errors import McpAwsError


@pytest.mark.parametrize(
    "arn,service,resource_type,resource_id",
    [
        ("arn:aws:ec2:us-east-1:123456789012:instance/i-abc", "ec2", "instance", "i-abc"),
        ("arn:aws:eks:eu-west-1:123456789012:cluster/prod", "eks", "cluster", "prod"),
        ("arn:aws:iam::123456789012:role/path/to/MyRole", "iam", "role", "MyRole"),
        ("arn:aws:s3:::my-bucket", "s3", None, "my-bucket"),
        (
            "arn:aws:elasticloadbalancing:us-east-1:1:loadbalancer/app/web/abc123",
            "elasticloadbalancing",
            "loadbalancer",
            "abc123",
        ),
    ],
)
def test_parse_arn(arn, service, resource_type, resource_id):
    parsed = parse_arn(arn)
    assert parsed.service == service
    assert parsed.resource_type == resource_type
    assert parsed.resource_id == resource_id


def test_region_and_account_are_optional():
    parsed = parse_arn("arn:aws:s3:::my-bucket")
    assert parsed.region is None
    assert parsed.account_id is None


def test_non_arn_is_rejected():
    with pytest.raises(McpAwsError, match="not an ARN"):
        parse_arn("i-abc123")
