"""Minimal ARN parsing, enough to route an ARN to the view that describes it."""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import McpAwsError


@dataclass(frozen=True)
class Arn:
    partition: str
    service: str
    region: str | None
    account_id: str | None
    resource_type: str | None
    resource_id: str

    @property
    def dispatch_key(self) -> str:
        return f"{self.service}:{self.resource_type or ''}"


def parse_arn(value: str) -> Arn:
    parts = value.split(":", 5)
    if len(parts) != 6 or parts[0] != "arn":
        raise McpAwsError(
            f"{value!r} is not an ARN "
            "(expected arn:partition:service:region:account:resource)"
        )

    _, partition, service, region, account_id, resource = parts

    # The resource half is service-specific: 'type/id', 'type:id', or a bare id.
    resource_type: str | None = None
    resource_id = resource
    for separator in ("/", ":"):
        if separator in resource:
            head, tail = resource.split(separator, 1)
            resource_type, resource_id = head, tail
            break

    return Arn(
        partition=partition,
        service=service,
        region=region or None,
        account_id=account_id or None,
        resource_type=resource_type,
        # Nested paths (role/path/to/name, cluster/name) keep only the final segment as
        # the id most describe-calls expect.
        resource_id=resource_id.split("/")[-1] if resource_type else resource_id,
    )
