"""Load, validate and index the YAML catalog.

Validation runs once at startup and is deliberately strict: a view that names a
non-existent operation, mistypes a request member, declares an un-paginatable operation
as paginated, or writes a broken JMESPath expression is a *build* failure, not something
a user discovers by asking a question. This is also where read-only stops being a
convention and becomes a property of the system — the engine can only ever invoke
operations that survived these checks.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable

import botocore.session
import jmespath
import yaml
from botocore import xform_name
from pydantic import ValidationError

from ..config import SETTINGS
from .models import ExpandSpec, View

log = logging.getLogger(__name__)

VIEWS_DIR = Path(__file__).parent / "views"

# Operations whose name proves they only read. Anything else cannot enter the catalog.
READ_ONLY_PREFIXES = (
    "describe_",
    "list_",
    "get_",
    "lookup_",
    "search_",
    "batch_get_",
    "head_",
    "query",
    "scan",
    "select_",
    "estimate_",
    "preview_",
    "simulate_",
    "test_",
    "validate_",
    "check_",
)

# Read-shaped but sensitive: these return secret material or customer data rather than
# configuration, so they stay out regardless of the prefix rule.
DENIED_OPERATIONS = {
    "s3:get_object",
    "s3:select_object_content",
    "secretsmanager:get_secret_value",
    "secretsmanager:batch_get_secret_value",
    "ssm:get_parameter",
    "ssm:get_parameters",
    "ssm:get_parameters_by_path",
    "kms:decrypt",
    "kms:generate_data_key",
    "dynamodb:get_item",
    "dynamodb:batch_get_item",
    "dynamodb:query",
    "dynamodb:scan",
    "sqs:receive_message",
    "cloudwatch_logs:get_log_events",
    "logs:get_log_events",
    "logs:filter_log_events",
    "iam:get_credential_report",
    "sts:get_session_token",
    "sts:get_federation_token",
    "cognito-idp:get_user",
    "lambda:get_function",  # presigned code download URL
}


class CatalogError(Exception):
    """Raised when the catalog is not internally consistent. Fatal at startup."""


def _is_read_only(operation: str) -> bool:
    return operation.startswith(READ_ONLY_PREFIXES)


class _Validator:
    def __init__(self) -> None:
        self._session = botocore.session.get_session()
        self._models: dict[str, Any] = {}
        self._paginators: dict[str, Any] = {}

    def service_model(self, client: str) -> Any:
        if client not in self._models:
            self._models[client] = self._session.get_service_model(client)
        return self._models[client]

    def operation_model(self, client: str, operation: str) -> Any:
        model = self.service_model(client)
        api_names = {xform_name(name): name for name in model.operation_names}
        if operation not in api_names:
            raise CatalogError(f"{client} has no operation '{operation}'")
        return model.operation_model(api_names[operation])

    def can_paginate(self, client: str, operation: str) -> bool:
        if client not in self._paginators:
            try:
                self._paginators[client] = self._session.get_paginator_model(client)
            except Exception:  # noqa: BLE001 - service with no paginators at all
                self._paginators[client] = None
        model = self._paginators[client]
        if model is None:
            return False
        api_name = "".join(part.title() for part in operation.split("_"))
        try:
            model.get_paginator(api_name)
        except ValueError:
            # Title-casing is a guess for names like describe_db_clusters; fall back to
            # the authoritative mapping from the service model.
            service = self.service_model(client)
            api_names = {xform_name(name): name for name in service.operation_names}
            try:
                model.get_paginator(api_names[operation])
            except (KeyError, ValueError):
                return False
        return True


def _check_jmespath(expression: str | None, where: str, problems: list[str]) -> None:
    if expression is None:
        return
    try:
        jmespath.compile(expression)
    except Exception as exc:  # noqa: BLE001
        problems.append(f"{where}: invalid JMESPath ({exc})")


def _check_operation(
    validator: _Validator,
    client: str,
    operation: str,
    where: str,
    problems: list[str],
) -> Any | None:
    if not _is_read_only(operation):
        problems.append(
            f"{where}: '{operation}' is not a read-only operation name "
            f"(must start with one of {', '.join(READ_ONLY_PREFIXES)})"
        )
        return None
    if f"{client}:{operation}" in DENIED_OPERATIONS:
        problems.append(f"{where}: '{client}:{operation}' is on the sensitive-read denylist")
        return None
    try:
        return validator.operation_model(client, operation)
    except CatalogError as exc:
        problems.append(f"{where}: {exc}")
    except Exception as exc:  # noqa: BLE001 - unknown service name, missing data files
        problems.append(f"{where}: cannot load model for client '{client}' ({exc})")
    return None


def _members(operation_model: Any) -> set[str]:
    shape = operation_model.input_shape
    return set(shape.members) if shape is not None else set()


def validate_view(view: View, validator: _Validator) -> list[str]:
    problems: list[str] = []
    where = f"view '{view.id}'"

    operation = _check_operation(validator, view.client, view.operation, where, problems)

    if view.paginate and operation is not None:
        if not validator.can_paginate(view.client, view.operation):
            problems.append(
                f"{where}: {view.client}.{view.operation} has no paginator; set paginate: false"
            )

    if operation is not None:
        members = _members(operation)
        for name, spec in view.params.items():
            source = spec.dynamic_filter_key
            if source is not None and source != "self" and source not in view.params:
                problems.append(
                    f"{where}: param '{name}' references undeclared param '{source}' "
                    "in its maps_to"
                )
            if spec.root_key not in members:
                problems.append(
                    f"{where}: param '{name}' maps to '{spec.root_key}', "
                    f"not a member of {view.operation} "
                    f"(valid: {', '.join(sorted(members)) or 'none'})"
                )
        for key in view.static_params:
            if key not in members:
                problems.append(f"{where}: static_params key '{key}' is not a member of {view.operation}")

    _check_jmespath(view.project, f"{where}.project", problems)

    if view.expand is not None:
        problems.extend(_validate_expand(view, view.expand, validator))
    elif view.project is None:
        problems.append(f"{where}: needs either 'project' or 'expand'")

    if view.detail_of is not None:
        if view.detail_of.count(":") != 1:
            problems.append(f"{where}: detail_of must look like '<service>:<resource-type>'")
        if view.detail_param is None:
            problems.append(f"{where}: detail_of requires detail_param")
        elif view.detail_param not in view.params:
            problems.append(f"{where}: detail_param '{view.detail_param}' is not a declared param")

    return problems


def _validate_expand(view: View, expand: ExpandSpec, validator: _Validator) -> list[str]:
    problems: list[str] = []
    where = f"view '{view.id}'.expand"
    client = expand.client or view.client

    child = _check_operation(validator, client, expand.operation, where, problems)
    if child is not None:
        members = _members(child)
        if expand.arg not in members:
            problems.append(
                f"{where}: arg '{expand.arg}' is not a member of {expand.operation} "
                f"(valid: {', '.join(sorted(members)) or 'none'})"
            )
        for key in expand.static_params:
            if key not in members:
                problems.append(f"{where}: static_params key '{key}' is not a member of {expand.operation}")
        for key in expand.inherit:
            if key not in members:
                problems.append(f"{where}: inherit key '{key}' is not a member of {expand.operation}")
            parent_members = {spec.root_key for spec in view.params.values()} | set(view.static_params)
            if key not in parent_members:
                problems.append(
                    f"{where}: inherit key '{key}' is never set by the parent call "
                    f"(parent sets: {', '.join(sorted(parent_members)) or 'nothing'})"
                )

    _check_jmespath(expand.arg_from, f"{where}.arg_from", problems)
    _check_jmespath(expand.project, f"{where}.project", problems)
    return problems


class Catalog:
    """The loaded set of views, indexed for listing and ARN dispatch."""

    def __init__(self, views: Iterable[View]) -> None:
        self._views: dict[str, View] = {}
        for view in views:
            self._views[view.id] = view
        self._by_detail: dict[str, View] = {
            view.detail_of: view for view in self._views.values() if view.detail_of
        }

    def __len__(self) -> int:
        return len(self._views)

    @property
    def services(self) -> list[str]:
        return sorted({view.service for view in self._views.values()})

    def get(self, view_id: str) -> View:
        view = self._views.get(view_id)
        if view is None:
            raise KeyError(view_id)
        return view

    def all(self) -> list[View]:
        return [self._views[key] for key in sorted(self._views)]

    def search(self, service: str | None = None, query: str | None = None) -> list[View]:
        results = self.all()
        if service:
            needle = service.lower()
            results = [v for v in results if v.service == needle]
        if query:
            terms = [t for t in query.lower().split() if t]
            results = [
                v
                for v in results
                if all(term in f"{v.id} {v.summary} {v.description or ''}".lower() for term in terms)
            ]
        return results

    def detail_view(self, service: str, resource_type: str) -> View | None:
        return self._by_detail.get(f"{service}:{resource_type}")


def _read_yaml_file(path: Path) -> list[View]:
    raw = yaml.safe_load(path.read_text()) or []
    if not isinstance(raw, list):
        raise CatalogError(f"{path}: expected a list of views, got {type(raw).__name__}")
    views: list[View] = []
    for index, entry in enumerate(raw):
        try:
            views.append(View(**{**entry, "source": str(path)}))
        except ValidationError as exc:
            raise CatalogError(f"{path} entry {index}: {exc}") from exc
        except TypeError as exc:
            raise CatalogError(f"{path} entry {index}: expected a mapping ({exc})") from exc
    return views


def load_catalog(directories: list[Path] | None = None, *, validate: bool = True) -> Catalog:
    dirs = directories if directories is not None else [VIEWS_DIR, *SETTINGS.extra_catalog_dirs]
    views: list[View] = []
    seen: dict[str, str] = {}

    for directory in dirs:
        if not directory.is_dir():
            log.warning("catalog directory %s does not exist; skipping", directory)
            continue
        for path in sorted(directory.glob("*.yaml")):
            for view in _read_yaml_file(path):
                previous = seen.get(view.id)
                if previous and previous != str(path):
                    # Later directories intentionally override earlier ones so an
                    # operator can patch a shipped view without forking the package.
                    log.info("view %s from %s overrides %s", view.id, path, previous)
                    views = [v for v in views if v.id != view.id]
                elif previous:
                    raise CatalogError(f"duplicate view id '{view.id}' within {path}")
                seen[view.id] = str(path)
                views.append(view)

    if validate:
        validator = _Validator()
        problems: list[str] = []
        for view in views:
            problems.extend(validate_view(view, validator))
        if problems:
            raise CatalogError(
                "catalog validation failed:\n  - " + "\n  - ".join(problems)
            )

    log.info("loaded %d catalog views from %s", len(views), ", ".join(str(d) for d in dirs))
    return Catalog(views)
