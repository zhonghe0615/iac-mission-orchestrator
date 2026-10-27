"""Reject clear contradictions between a work order and its operator-selected fields."""

import re

from .plan import Constraints


ASSET_REF = re.compile(r"\bT-?(\d{3})\b", re.IGNORECASE)
NO_REPAIR = re.compile(
    r"\b(?:(?:do not|don't)\s+(?:perform\s+)?(?:any\s+)?repairs?"
    r"|no\s+(?:any\s+)?repairs?(?!\s+(?:have|has|had|were|was|needed|required|planned|scheduled"
    r"|are\s+(?:needed|required|planned|scheduled))))\b"
    r"(?![^.!?;]*\b(?:unless|until|without|before)\b)",
    re.IGNORECASE,
)
REPAIR_REQUEST = re.compile(
    r"(?:^|[.!?;:,]\s*|\b(?:and|then|please|to|must|should)\s+)"
    r"(?:repair|replace|seal)\s+(?:the|it|only|if|now)\b",
    re.IGNORECASE,
)


def work_order_conflict(work_order: str, constraints: Constraints) -> str | None:
    """Catch explicit asset IDs and repair instructions; leave ambiguous prose to the operator."""
    if not constraints.target:
        return "mission has no target asset; copy it into a new work order and select the target"
    assets = {f"T-{match}" for match in ASSET_REF.findall(work_order)}
    chosen = constraints.target.removeprefix("tank-").upper().replace("-", "")
    if len(assets) > 1:
        return f"work order names multiple target assets ({', '.join(sorted(assets))}); select one asset per mission"
    if assets and chosen not in {asset.replace("-", "") for asset in assets}:
        return f"work order names {next(iter(assets))}, but target asset is {constraints.target}; correct the text or target"
    if constraints.allow_repair and NO_REPAIR.search(work_order):
        return "work order prohibits repairs, but Allow repairs is selected; correct the text or permission"
    if not constraints.allow_repair and REPAIR_REQUEST.search(work_order) and not NO_REPAIR.search(work_order):
        return "work order requests repair, but Allow repairs is off; correct the text or permission"
    return None
