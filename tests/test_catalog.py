"""Catalog conformance — the test that keeps coverage growth safe.

Every shipped view is checked against the real botocore service models, so a typo in a
YAML file fails here rather than in front of a user.
"""

from __future__ import annotations

import pytest
import yaml

from mcp_aws.catalog.loader import (
    DENIED_OPERATIONS,
    READ_ONLY_PREFIXES,
    VIEWS_DIR,
    CatalogError,
    _Validator,
    load_catalog,
    validate_view,
)
from mcp_aws.catalog.models import View


def test_shipped_catalog_validates():
    catalog = load_catalog()
    assert len(catalog) > 0


def test_every_view_is_individually_valid(catalog):
    validator = _Validator()
    problems = []
    for view in catalog.all():
        problems.extend(validate_view(view, validator))
    assert problems == []


def test_every_view_operation_is_read_only(catalog):
    for view in catalog.all():
        assert view.operation.startswith(READ_ONLY_PREFIXES), view.id
        assert f"{view.client}:{view.operation}" not in DENIED_OPERATIONS
        if view.expand:
            child_client = view.expand.client or view.client
            assert view.expand.operation.startswith(READ_ONLY_PREFIXES), view.id
            assert f"{child_client}:{view.expand.operation}" not in DENIED_OPERATIONS


def test_every_view_has_a_useful_summary(catalog):
    for view in catalog.all():
        assert view.summary and len(view.summary) > 10, view.id
        assert "\n" not in view.summary, f"{view.id}: summary must stay one line"


def test_detail_views_cover_distinct_resource_types(catalog):
    seen: dict[str, str] = {}
    for view in catalog.all():
        if view.detail_of:
            assert view.detail_of not in seen, (
                f"{view.id} and {seen[view.detail_of]} both claim {view.detail_of}"
            )
            seen[view.detail_of] = view.id


def test_yaml_files_are_lists_of_mappings():
    for path in VIEWS_DIR.glob("*.yaml"):
        entries = yaml.safe_load(path.read_text())
        assert isinstance(entries, list), path
        assert all(isinstance(entry, dict) for entry in entries), path


def _view(**overrides) -> View:
    base = dict(
        id="ec2.test.list",
        summary="a test view with a long enough summary",
        client="ec2",
        operation="describe_instances",
        project="Reservations[]",
    )
    return View(**{**base, **overrides})


def test_write_operation_is_rejected():
    problems = validate_view(_view(operation="terminate_instances"), _Validator())
    assert any("not a read-only operation" in p for p in problems)


def test_sensitive_read_is_rejected():
    problems = validate_view(
        _view(id="s3.object.get", client="s3", operation="get_object", project="Body"),
        _Validator(),
    )
    assert any("denylist" in p for p in problems)


def test_unknown_operation_is_rejected():
    problems = validate_view(_view(operation="describe_unicorns"), _Validator())
    assert any("no operation" in p for p in problems)


def test_bad_param_target_is_rejected():
    problems = validate_view(
        _view(params={"nope": {"maps_to": "NotAMember"}}), _Validator()
    )
    assert any("not a member" in p for p in problems)


def test_broken_jmespath_is_rejected():
    problems = validate_view(_view(project="Reservations[].{"), _Validator())
    assert any("invalid JMESPath" in p for p in problems)


def test_unpaginatable_operation_declared_as_paginated_is_rejected():
    problems = validate_view(
        _view(operation="describe_account_attributes", paginate=True, project="AccountAttributes[]"),
        _Validator(),
    )
    assert any("no paginator" in p for p in problems)


def test_loader_reports_all_problems_at_once(tmp_path):
    (tmp_path / "bad.yaml").write_text(
        "- id: ec2.a.list\n"
        "  summary: first broken view here\n"
        "  client: ec2\n"
        "  operation: delete_vpc\n"
        "  project: 'Vpcs[]'\n"
        "- id: ec2.b.list\n"
        "  summary: second broken view here\n"
        "  client: ec2\n"
        "  operation: describe_nothing\n"
        "  project: 'Nope[]'\n"
    )
    with pytest.raises(CatalogError) as excinfo:
        load_catalog([tmp_path])
    message = str(excinfo.value)
    assert "ec2.a.list" in message and "ec2.b.list" in message
