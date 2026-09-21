"""The resource layer: vocabularies a client can pick from, and the members they buy.

Tools remain the path a model uses. Resources are for the human side — browsing,
`@`-mentioning, and the picker an MCP gateway builds from a *listing* paired with a
*template*. That pairing is a rule, not a convention: a template's fixed prefix, up to
its first `{`, must be the listing's URI, and nothing else in a server's resource space
is ever read on spec. So the URIs here are arranged in pairs:

    aws://accounts                     ->  aws://accounts/{profile}
    aws://catalog                      ->  aws://catalog/{view_id}
    aws://services                     ->  aws://services/{service}/views  (narrows)
    aws://services/{service}/views     ->  aws://catalog/{view_id}
    aws://regions                      ->  aws://regions/{region}

and `aws://query/{profile}/{region}/{view_id}` pairs with nothing on purpose: a listing
beside it would be read whenever someone opens this server, and reading it would mean
running AWS queries nobody asked for. Its three variables already have vocabularies of
their own.

Two rules the handlers below keep:

* **A listing costs nothing to read.** It is fetched on open, on Reread and on every
  `list_changed`, so every value here comes from local config, bundled endpoint data or
  the in-memory catalog. Live calls live behind a member URI.
* **A URI this server never handed out is an error, not an empty body.** An empty enum
  says "there is nothing here", which is a different and usually wrong fact.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Callable, Iterator, NoReturn

from mcp.server.mcpserver.exceptions import ResourceError, ResourceNotFoundError

from .aws import regions
from .aws.profiles import list_profile_names, resolve_profile
from .catalog.loader import Catalog
from .errors import McpAwsError
from .tools import tool_describe_view
from .vocabulary import (
    enum_body,
    profile_choices,
    region_choices,
    service_choices,
    view_choices,
)

ACCOUNTS = "aws://accounts"
ACCOUNT_TEMPLATE = "aws://accounts/{profile}"
CATALOG = "aws://catalog"
VIEW_TEMPLATE = "aws://catalog/{view_id}"
SERVICES = "aws://services"
SERVICE_VIEWS_TEMPLATE = "aws://services/{service}/views"
REGIONS = "aws://regions"
REGION_TEMPLATE = "aws://regions/{region}"
QUERY_TEMPLATE = "aws://query/{profile}/{region}/{view_id}"


def _dump(payload: Any) -> str:
    return json.dumps(payload, indent=2, default=str)


def _not_found(message: str) -> NoReturn:
    raise ResourceNotFoundError(message)


@contextmanager
def _as_resource_error() -> Iterator[None]:
    """Report an anticipated failure as one.

    A tool returns its error as content; a resource raises it as protocol. Letting an
    McpAwsError escape a resource handler would have the SDK treat it as a crash — the
    client gets "Error reading resource ..." and the part that says how to fix it is
    withheld.
    """
    try:
        yield
    except McpAwsError as exc:
        raise ResourceError(str(exc)) from exc


def register_resources(server: Any, catalog: Catalog, runner: Callable[..., dict]) -> None:
    # Registration order is match order for templates, so the broad one goes last.

    @server.resource(
        ACCOUNTS,
        name="AWS accounts",
        title="AWS accounts",
        description=(
            "The AWS profiles (accounts) this server can read. Values fill a 'profile' "
            f"argument; one of them buys {ACCOUNT_TEMPLATE}. Read from local config only."
        ),
        mime_type="application/json",
    )
    def accounts_listing() -> str:
        return _dump(
            enum_body(
                profile_choices(),
                description=(
                    "Configured AWS profiles. A label shows the account behind a profile "
                    "once it has been resolved; reading one resolves it."
                ),
                read_one=ACCOUNT_TEMPLATE,
            )
        )

    @server.resource(
        ACCOUNT_TEMPLATE,
        name="AWS account",
        title="AWS account",
        description="One profile resolved to account id, alias, default region and status.",
        mime_type="application/json",
    )
    def account_resource(profile: str) -> str:
        if profile not in list_profile_names():
            _not_found(f"No configured profile '{profile}'; see {ACCOUNTS}")
        with _as_resource_error():
            return _dump(resolve_profile(profile))

    @server.resource(
        CATALOG,
        name="AWS catalog",
        title="AWS catalog",
        description=(
            "Every read-only view this server offers. Values fill a 'view_id' argument; "
            f"one of them buys {VIEW_TEMPLATE}."
        ),
        mime_type="application/json",
    )
    def catalog_listing() -> str:
        return _dump(
            enum_body(
                view_choices(catalog),
                description="The read-only AWS questions this server can answer.",
                read_one=VIEW_TEMPLATE,
            )
        )

    @server.resource(
        VIEW_TEMPLATE,
        name="AWS view",
        title="AWS view",
        description="One view's parameters, output shape and the AWS call behind it.",
        mime_type="application/json",
    )
    def view_resource(view_id: str) -> str:
        try:
            catalog.get(view_id)
        except KeyError:
            _not_found(f"No such view '{view_id}'; see {CATALOG}")
        with _as_resource_error():
            return _dump(tool_describe_view(catalog, view_id))

    @server.resource(
        SERVICES,
        name="AWS services",
        title="AWS services",
        description=(
            "The AWS services this server has views for. Values fill a 'service' argument; "
            f"one of them narrows to {SERVICE_VIEWS_TEMPLATE}."
        ),
        mime_type="application/json",
    )
    def services_listing() -> str:
        return _dump(
            enum_body(
                service_choices(catalog),
                description="Services covered by the catalog.",
                narrows=SERVICE_VIEWS_TEMPLATE,
            )
        )

    @server.resource(
        SERVICE_VIEWS_TEMPLATE,
        name="AWS service views",
        title="AWS service views",
        description=(
            "The views of one service — the second level of the services cascade. Read at "
            f"a URI {SERVICES} handed over, never one assembled on a hunch."
        ),
        mime_type="application/json",
    )
    def service_views_listing(service: str) -> str:
        if service not in catalog.services:
            # An empty enum here would read as "this service has no views", which is a
            # different and wrong fact.
            _not_found(f"No such service '{service}'; see {SERVICES}")
        return _dump(
            enum_body(
                view_choices(catalog, service=service),
                description=f"The read-only views for {service}.",
                read_one=VIEW_TEMPLATE,
            )
        )

    @server.resource(
        REGIONS,
        name="AWS regions",
        title="AWS regions",
        description=(
            "Regions of the standard partition, from bundled endpoint data. Values fill a "
            f"'region' argument; one of them buys {REGION_TEMPLATE}."
        ),
        mime_type="application/json",
    )
    def regions_listing() -> str:
        return _dump(
            enum_body(
                region_choices(),
                description=(
                    "AWS regions. Whether a given account has one enabled is a live "
                    "question; run the 'account.regions' view for that."
                ),
                read_one=REGION_TEMPLATE,
            )
        )

    @server.resource(
        REGION_TEMPLATE,
        name="AWS region",
        title="AWS region",
        description="One region: partition, geography and which profiles default to it.",
        mime_type="application/json",
    )
    def region_resource(region: str) -> str:
        detail = regions.region_detail(region)
        if detail is None:
            _not_found(f"No such region '{region}'; see {REGIONS}")
        return _dump(detail)

    @server.resource(
        QUERY_TEMPLATE,
        name="AWS view result",
        title="AWS view result",
        description=(
            "Run a catalog view against one profile and region. This is a LIVE AWS read, "
            "which is why no listing is published beside it. Use 'default' as the region "
            "to take the profile's own."
        ),
        mime_type="application/json",
    )
    def query_resource(profile: str, region: str, view_id: str) -> str:
        try:
            view = catalog.get(view_id)
        except KeyError:
            _not_found(f"No such view '{view_id}'; see {CATALOG}")
        # 'default' lets a URI stay readable when the profile's own region is wanted.
        resolved = None if region in ("default", "-") else region
        with _as_resource_error():
            return _dump(runner(view, profile=profile, region=resolved))
