"""Pydantic models for a catalog view.

A view is one read-only AWS question, declared as data: which client, which operation,
which parameters callers may set, and how the raw response is projected down to
something small enough to put in a model's context.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

VIEW_ID_RE = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)+$")

ParamType = Literal["string", "integer", "boolean", "string[]"]


class ParamSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: ParamType = "string"
    description: str = ""
    required: bool = False
    enum: list[str] | None = None
    default: Any = None
    maps_to: str = Field(
        description="Target in the AWS request: 'InstanceIds', 'Foo.Bar', or 'Filters[tag-key]'"
    )
    filter_style: Literal["name_values", "key_values"] = Field(
        default="name_values",
        description="Shape of a bracket filter entry: EC2's {Name, Values} or the "
        "{Key, Values} used by tagging APIs",
    )

    @property
    def root_key(self) -> str:
        """The top-level request member this parameter writes into."""
        head = self.maps_to.split(".", 1)[0]
        return head.split("[", 1)[0]

    @property
    def filter_name(self) -> str | None:
        """Literal filter key for 'Filters[instance-state-name]' style targets."""
        match = re.match(r"^[A-Za-z0-9]+\[(.+)\]$", self.maps_to)
        if match is None or match.group(1).startswith("$"):
            return None
        return match.group(1)

    @property
    def dynamic_filter_key(self) -> str | None:
        """For 'TagFilters[$tag_key]' targets: the param supplying the filter key.

        '$self' means this param's own value is the key (a key-only filter); any other
        name points at a sibling param. Tagging-style APIs need this because the filter
        key is user data, not part of the API contract.
        """
        match = re.match(r"^[A-Za-z0-9]+\[\$(.+)\]$", self.maps_to)
        return match.group(1) if match else None


class ExpandSpec(BaseModel):
    """Declarative fan-out: run a per-item follow-up call and project each result.

    Needed wherever AWS splits a list from its detail (eks list_clusters +
    describe_cluster). Bounded on both axes so a large account cannot stall a turn.
    """

    model_config = ConfigDict(extra="forbid")

    operation: str
    client: str | None = None
    arg: str = Field(description="Request member on the follow-up call that receives the item")
    arg_from: str = Field(default="@", description="JMESPath on each item producing the arg value")
    project: str | None = None
    max_items: int = 100
    static_params: dict[str, Any] = Field(default_factory=dict)
    inherit: list[str] = Field(
        default_factory=list,
        description="Request members copied from the parent call, for APIs whose detail "
        "call needs the parent key too (eks describe_nodegroup needs ClusterName)",
    )


class View(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    summary: str = Field(description="One line; this is what the catalog listing shows")
    description: str | None = None
    client: str
    operation: str
    paginate: bool = False
    regional: bool = True
    global_region: str = "us-east-1"
    params: dict[str, ParamSpec] = Field(default_factory=dict)
    static_params: dict[str, Any] = Field(default_factory=dict)
    project: str | None = None
    expand: ExpandSpec | None = None
    returns: str | None = Field(default=None, description="Human note on the shape of each item")
    detail_of: str | None = Field(
        default=None, description="'<arn-service>:<resource-type>' this view resolves ARNs for"
    )
    detail_param: str | None = Field(
        default=None, description="Which declared param receives the identifier from an ARN"
    )
    detail_from: Literal["resource_id", "arn"] = Field(
        default="resource_id",
        description="Whether detail_param takes the ARN's resource id or the whole ARN "
        "(elbv2 and similar services describe by ARN)",
    )
    source: str | None = Field(default=None, exclude=True)

    @field_validator("id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        if not VIEW_ID_RE.match(value):
            raise ValueError(f"view id {value!r} must be dotted lowercase, e.g. 'ec2.instances.list'")
        return value

    @property
    def service(self) -> str:
        return self.id.split(".", 1)[0]

    def listing_entry(self) -> dict[str, str]:
        return {"view_id": self.id, "summary": self.summary}
