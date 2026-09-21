"""Logging that never touches stdout.

stdout is the JSON-RPC channel for a stdio MCP server. A single stray byte on it
corrupts the framing and the gateway drops the connection with no useful error, so
every handler here is pinned to stderr and propagation to any inherited root handler
is cut off.
"""

from __future__ import annotations

import logging
import os
import sys

_CONFIGURED = False


def configure() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    level = os.environ.get("MCP_AWS_LOG_LEVEL", "INFO").upper()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    # boto3/botocore are chatty at DEBUG and would drown the real signal.
    for noisy in ("botocore", "boto3", "urllib3", "s3transfer"):
        logging.getLogger(noisy).setLevel(max(logging.WARNING, root.level))

    _CONFIGURED = True
