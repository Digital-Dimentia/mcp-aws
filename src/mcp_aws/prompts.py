"""Two prompts, written so their arguments are the vocabulary's variables.

A prompt argument named `profile` is filled by the same chips that fill the `profile`
argument of a tool, because a gateway matches a value group to a field by name. That is
the entire reason these take `profile` / `region` / `arn` rather than prettier names.
"""

from __future__ import annotations

from typing import Any

from .config import SETTINGS

#: Argument names per prompt, so `completions.py` can answer for a prompt ref without
#: importing the handlers or guessing.
PROMPT_ARGUMENTS: dict[str, tuple[str, ...]] = {
    "account_overview": ("profile", "region"),
    "investigate_resource": ("arn", "profile"),
}


def prompt_name(stem: str) -> str:
    return f"{SETTINGS.tool_prefix}{stem}"


def register_prompts(server: Any) -> None:
    prefix = SETTINGS.tool_prefix

    @server.prompt(
        name=prompt_name("account_overview"),
        title="AWS account overview",
        description="Survey one account: identity, then compute, network and load balancing.",
    )
    def account_overview(profile: str, region: str = "") -> str:
        where = f" in {region}" if region else " in the profile's default region"
        return (
            f"Give me an overview of the AWS account behind profile '{profile}'{where}.\n\n"
            f"Work through it with the catalog rather than guessing view ids:\n"
            f"1. {prefix}list_accounts to confirm the profile resolves.\n"
            f"2. {prefix}catalog(service=...) for each area you cover.\n"
            f"3. {prefix}query for the views that matter — at minimum account identity, EC2\n"
            f"   instances, VPCs and security groups, load balancers, and any EKS clusters.\n\n"
            f"Every result is capped; when one comes back with truncated=true, follow\n"
            f"next_cursor before drawing a conclusion about totals. Report what is actually\n"
            f"there, and say plainly which areas you did not cover."
        )

    @server.prompt(
        name=prompt_name("investigate_resource"),
        title="Investigate an AWS resource",
        description="Start from an ARN, then widen to the resources around it.",
    )
    def investigate_resource(arn: str, profile: str = "") -> str:
        account = f" using profile '{profile}'" if profile else ""
        return (
            f"Investigate the AWS resource {arn}{account}.\n\n"
            f"1. {prefix}read_resource on the ARN for its detail view.\n"
            f"2. {prefix}catalog(service=<the ARN's service>) to see what else can be asked,\n"
            f"   then {prefix}query for the neighbours that explain it — the security groups,\n"
            f"   subnets, target groups or node groups it is attached to.\n\n"
            f"Stay inside the account the ARN names unless I say otherwise, and tell me if\n"
            f"the ARN's resource type has no view rather than substituting a near miss."
        )
