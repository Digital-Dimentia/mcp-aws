"""Region knowledge, read entirely from local data.

Everything here answers from botocore's bundled endpoint metadata and the shared AWS
config file. That is a deliberate constraint rather than an optimisation: these values
feed the vocabulary listings, which a gateway reads whenever someone opens the server,
and a listing that calls AWS to describe itself is a listing that costs money to look at.
`ec2:DescribeRegions` would be more accurate about opt-in regions, and is what
`account.regions` in the catalog is for.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

import botocore.session

from .clients import default_region

log = logging.getLogger(__name__)

#: `ec2` is the broadest service in the endpoint data, so its region set is the closest
#: thing to "every region in this partition".
_REFERENCE_SERVICE = "ec2"

PARTITIONS = ("aws", "aws-cn", "aws-us-gov")


@lru_cache(maxsize=None)
def list_regions(partition: str = "aws") -> list[str]:
    """Every region of one partition, from bundled endpoint data."""
    try:
        session = botocore.session.get_session()
        return sorted(session.get_available_regions(_REFERENCE_SERVICE, partition_name=partition))
    except Exception as exc:  # noqa: BLE001 - endpoint data is bundled; never fail a listing
        log.warning("could not enumerate regions for partition %s: %s", partition, exc)
        return []


def partition_for(region: str) -> str | None:
    """The partition a region belongs to, or None when it is not a known region."""
    for partition in PARTITIONS:
        if region in list_regions(partition):
            return partition
    return None


def _profiles_defaulting_to(region: str) -> list[str]:
    from .profiles import list_profile_names  # circular at module level

    found = []
    for profile in list_profile_names():
        try:
            if default_region(profile) == region:
                found.append(profile)
        except Exception:  # noqa: BLE001 - one unreadable profile must not fail the read
            continue
    return found


def region_detail(region: str) -> dict[str, Any] | None:
    """One region as it reads at the end of the vocabulary, or None if unknown.

    None rather than an empty body: the caller turns it into a not-found, because a
    region this server never published is a different fact from a region with nothing
    in it.
    """
    partition = partition_for(region)
    if partition is None:
        return None
    return {
        "region": region,
        "partition": partition,
        "geography": region.split("-")[0],
        "profiles_defaulting_here": _profiles_defaulting_to(region),
        "note": (
            "Enumerated offline from botocore endpoint data. Whether this account has the "
            "region enabled is a live question — run the 'account.regions' view for that."
        ),
    }
