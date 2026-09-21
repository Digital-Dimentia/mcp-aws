from __future__ import annotations

import json

import boto3
import botocore.session
import pytest
from botocore.config import Config
from botocore.stub import Stubber

from mcp_aws.aws import clients, profiles
from mcp_aws.catalog.engine import reset_cache
from mcp_aws.catalog.loader import load_catalog
from mcp_aws.server import build_server, dump_tool_surface


@pytest.fixture(autouse=True)
def _no_result_cache(monkeypatch):
    """The TTL cache would mask per-test differences; disable it everywhere."""
    monkeypatch.setenv("MCP_AWS_CACHE_TTL", "0")
    reset_cache()
    yield
    reset_cache()


@pytest.fixture(scope="session")
def catalog():
    return load_catalog()


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """No test may inherit the developer's own AWS environment.

    The autouse cache reset above covers the engine only; the profile identity cache and
    the session cache are module globals that would otherwise leak a real account id from
    one test into another's vocabulary labels.
    """
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    clients.reset()
    profiles.reset()
    yield
    clients.reset()
    profiles.reset()


@pytest.fixture
def fake_profiles(monkeypatch):
    """Three configured profiles, whatever is in the developer's ~/.aws/config.

    Patched at botocore's own accessor rather than at `list_profile_names`, because a
    module that did `from .profiles import list_profile_names` would keep the original
    binding and slip past a patch applied higher up.
    """
    names = ["dev", "prod", "sandbox"]
    monkeypatch.setattr(
        botocore.session.Session, "available_profiles", property(lambda self: list(names))
    )
    return names


@pytest.fixture
def no_aws_calls(monkeypatch):
    """Make any attempt to reach AWS both fail and leave a trace.

    Recording matters as much as raising: `resolve_profile` catches bare Exception by
    design, so a listing that started calling AWS would otherwise quietly report every
    profile as 'unavailable' and the test would pass. Client *creation* is the line —
    reading a region out of ~/.aws/config is local and stays allowed.
    """
    attempts: list[tuple] = []

    def boom(*args, **kwargs):
        attempts.append((args, kwargs))
        raise AssertionError(f"live AWS call: {args!r}")

    monkeypatch.setattr("mcp_aws.aws.clients.client_for", boom)
    monkeypatch.setattr("mcp_aws.aws.profiles.client_for", boom)
    monkeypatch.setattr("mcp_aws.catalog.engine.client_for", boom)
    monkeypatch.setattr(boto3.Session, "client", boom)
    monkeypatch.setattr(botocore.session.Session, "create_client", boom)
    return attempts


@pytest.fixture
def server(catalog, fake_profiles):
    return build_server(catalog)


@pytest.fixture(scope="session")
def tool_surface():
    return json.loads(dump_tool_surface())


@pytest.fixture
def stub_client(monkeypatch):
    """Install a stubbed boto3 client in place of the engine's client factory.

    Returns a function: stub_client("ec2", region="us-east-1") -> (client, Stubber).
    """
    created: dict[tuple[str, str | None], object] = {}

    def factory(service: str, region: str | None = "us-east-1"):
        client = boto3.client(
            service,
            region_name=region or "us-east-1",
            aws_access_key_id="testing",
            aws_secret_access_key="testing",
            config=Config(retries={"max_attempts": 1}),
        )
        stubber = Stubber(client)
        stubber.activate()
        created[(service, region)] = client

        def fake_client_for(profile, svc, rgn=None):
            return created[(svc, rgn)] if (svc, rgn) in created else client

        monkeypatch.setattr("mcp_aws.catalog.engine.client_for", fake_client_for)
        return client, stubber

    return factory
