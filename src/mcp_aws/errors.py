"""botocore failures translated into something a model can act on."""

from __future__ import annotations

from botocore.exceptions import (
    ClientError,
    NoCredentialsError,
    ProfileNotFound,
    SSOTokenLoadError,
    TokenRetrievalError,
    UnauthorizedSSOTokenError,
)


class McpAwsError(Exception):
    """Raised for conditions the caller can fix; message goes back as tool output."""


_CREDENTIAL_ERRORS = (
    NoCredentialsError,
    SSOTokenLoadError,
    TokenRetrievalError,
    UnauthorizedSSOTokenError,
)

_CODE_HINTS = {
    "AccessDenied": "the profile's principal lacks the required read permission",
    "AccessDeniedException": "the profile's principal lacks the required read permission",
    "UnauthorizedOperation": "the profile's principal lacks the required read permission",
    "ExpiredToken": "credentials expired; refresh them (aws sso login --profile {profile})",
    "ExpiredTokenException": "credentials expired; refresh them (aws sso login --profile {profile})",
    "RequestExpired": "credentials expired; refresh them (aws sso login --profile {profile})",
    "InvalidClientTokenId": "credentials are not valid for this account",
    "OptInRequired": "the region is not enabled for this account",
    "AuthFailure": "credentials rejected; check the profile and region",
    "ResourceNotFoundException": "no such resource in this account/region",
    "AWSOrganizationsNotInUseException": "this account is not part of an AWS Organization",
    "AccessDeniedForDependencyException": "the profile cannot read the dependent service",
}


def describe_failure(exc: Exception, *, profile: str, region: str | None = None) -> str:
    """One line, naming the profile/region and what to do about it."""
    where = f"profile '{profile}'" + (f" in {region}" if region else "")

    if isinstance(exc, ProfileNotFound):
        return f"No such profile '{profile}'. Call the list-accounts tool to see what is configured."

    if isinstance(exc, _CREDENTIAL_ERRORS):
        return (
            f"No usable credentials for {where}: {exc}. "
            f"For SSO profiles run: aws sso login --profile {profile}"
        )

    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        message = exc.response.get("Error", {}).get("Message", str(exc))
        hint = _CODE_HINTS.get(code)
        detail = f" — {hint.format(profile=profile)}" if hint else ""
        return f"AWS returned {code} for {where}: {message}{detail}"

    return f"Request failed for {where}: {type(exc).__name__}: {exc}"
