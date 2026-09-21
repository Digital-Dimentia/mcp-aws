"""Named profiles are this server's notion of an 'account'.

Profiles are enumerated from the shared AWS config/credentials files and resolved
concurrently to an account id + alias. Resolution is best-effort by design: one expired
SSO profile must never fail the whole listing, it just reports how to fix itself.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import botocore.session
from botocore.exceptions import ClientError

from ..config import SETTINGS
from ..errors import describe_failure
from .clients import client_for, default_region

log = logging.getLogger(__name__)

_identity_cache: dict[str, dict[str, Any]] = {}


def list_profile_names() -> list[str]:
    """Configured profile names, filtered by MCP_AWS_PROFILES when set."""
    try:
        names = botocore.session.Session().available_profiles
    except Exception as exc:  # noqa: BLE001 - a broken config file must not kill startup
        log.warning("could not read AWS config: %s", exc)
        return []
    return sorted(name for name in names if SETTINGS.profile_allowed(name))


def cached_identity(profile: str) -> dict[str, Any] | None:
    """The identity already resolved for this profile, or None.

    Never calls AWS. Vocabulary listings are read whenever a client opens the server, so
    they label a profile from whatever is already known and leave resolution to a read of
    the profile itself.
    """
    return _identity_cache.get(profile)


def _alias_for(profile: str) -> str | None:
    try:
        aliases = client_for(profile, "iam").list_account_aliases().get("AccountAliases", [])
        return aliases[0] if aliases else None
    except ClientError:
        return None  # iam:ListAccountAliases is commonly denied; not worth reporting
    except Exception:  # noqa: BLE001
        return None


def resolve_profile(profile: str, *, refresh: bool = False) -> dict[str, Any]:
    """Identify one profile. Never raises; failures are reported in the payload."""
    if not refresh and profile in _identity_cache:
        return _identity_cache[profile]

    region = None
    try:
        region = default_region(profile)
        identity = client_for(profile, "sts", region).get_caller_identity()
        result = {
            "profile": profile,
            "account_id": identity.get("Account"),
            "account_alias": _alias_for(profile),
            "arn": identity.get("Arn"),
            "default_region": region,
            "status": "ok",
        }
    except Exception as exc:  # noqa: BLE001 - every failure mode is reportable data here
        result = {
            "profile": profile,
            "account_id": None,
            "account_alias": None,
            "arn": None,
            "default_region": region,
            "status": "unavailable",
            "detail": describe_failure(exc, profile=profile, region=region),
        }

    # Only successful identities are cached; a profile the user just re-authenticated
    # should start working without restarting the server.
    if result["status"] == "ok":
        _identity_cache[profile] = result
    return result


def list_accounts(*, refresh: bool = False) -> list[dict[str, Any]]:
    names = list_profile_names()
    if not names:
        return []
    workers = min(len(names), 8)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda name: resolve_profile(name, refresh=refresh), names))


def profile_for_account_id(account_id: str) -> str | None:
    """Reverse lookup used by ARN dispatch. Prefers already-resolved profiles."""
    for identity in _identity_cache.values():
        if identity.get("account_id") == account_id:
            return identity["profile"]
    for identity in list_accounts():
        if identity.get("account_id") == account_id:
            return identity["profile"]
    return None


def reset() -> None:
    _identity_cache.clear()
