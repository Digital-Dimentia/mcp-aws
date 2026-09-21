from __future__ import annotations

import boto3
import pytest
from botocore.config import Config
from botocore.stub import Stubber

from mcp_aws.catalog.engine import reset_cache
from mcp_aws.catalog.loader import load_catalog


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
