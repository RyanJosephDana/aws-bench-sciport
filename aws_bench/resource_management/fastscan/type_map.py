"""Noun-matching helpers that feed :meth:`FastScanManager.project`'s CFN-type attribution.

project() attributes an unpinned discovery to a CFN type when the type's service endpoint matches
the lister's service and the type's resource nouns overlap the lister op's noun. Each function here
computes one side of that comparison (or the sibling-collision types project() must skip).
"""

from __future__ import annotations

import re

# CFN service token (lowercased, hyphen-stripped) → boto3 service endpoint, for genuine
# renames/aliases. Hyphenation differences are handled by the hyphen-insensitive match.
_CFN_TO_ENDPOINT_ALIASES: dict[str, str] = {
    "msk": "kafka",
    "amazonmq": "mq",
    "cognito": "cognito-idp",
    "lex": "lexv2-models",
    "aps": "amp",
    "inspectorv2": "inspector2",
    "macie": "macie2",
    "sso": "sso-admin",
    "elasticloadbalancingv2": "elbv2",
    "elasticloadbalancing": "elb",
    "certificatemanager": "acm",
    "opensearchservice": "opensearch",
    "kinesisfirehose": "firehose",
    "elasticsearch": "es",
}

# CFN types fast-scan must NOT attribute by (endpoint, noun): their only reachable lister is
# a *sibling* type's lister that emits the same identifiers, so noun-matching would double-type
# the sibling's resources onto this type. That produces phantom "resources" (and, because the
# baseline merges regions while verify compares per-region, spurious "new resource" verify
# failures). Neither can be told apart from its sibling by identifier, so the honest projection
# is to leave the type unscanned rather than claim the sibling's resources as this type:
#   * AWS::WorkspacesInstances::Volume — the EC2 ``DescribeVolumes`` lister (noun ``volumes``)
#     also feeds AWS::EC2::Volume; both share the ``vol-…`` ids, so plain EBS volumes were
#     being reported as WorkSpaces volumes too.
#   * AWS::ECR::PublicRepository — the ecr ``DescribeRepositories`` lister (noun
#     ``repositories``) feeds AWS::ECR::Repository; ECR Public lives on a different endpoint
#     (``ecr-public``), so a private CDK repo was being double-typed as a public one.
_UNSCANNABLE_BY_NOUN: frozenset[str] = frozenset(
    {
        "AWS::WorkspacesInstances::Volume",
        "AWS::ECR::PublicRepository",
    }
)


def collides_with_sibling_type(cfn_type: str) -> bool:
    """True if project() must skip this type: its only lister is a sibling's (same identifiers)."""
    return cfn_type in _UNSCANNABLE_BY_NOUN


def cfn_type_service_endpoint(cfn_type: str, known_services: set[str]) -> str | None:
    """The boto3 service endpoint a CFN type maps to (feeds project()'s per-service matching)."""
    parts = cfn_type.split("::")
    if len(parts) < 2 or parts[0] != "AWS":
        return None
    token = parts[1].lower()
    stripped = token.replace("-", "")
    alias = _CFN_TO_ENDPOINT_ALIASES.get(stripped)
    if alias and alias in known_services:
        return alias
    if token in known_services:
        return token
    for svc in known_services:
        if svc.replace("-", "") == stripped:
            return svc
    return None


def cfn_type_resource_nouns(cfn_type: str) -> set[str]:
    """Lowercased nouns a CFN type's resource part can match; project() compares to op nouns."""
    parts = cfn_type.split("::")
    if len(parts) < 3:
        return set()
    res = parts[2]
    words = re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z][a-z]+|[A-Z]+|[a-z]+|[0-9]+", res)
    bases = {res.lower()}
    if words:
        bases.add("".join(words).lower())
        bases.add(words[-1].lower())
        if len(words) >= 2:
            bases.add("".join(words[-2:]).lower())
    out: set[str] = set()
    for base in bases:
        out.add(base)
        out.add(base + "s")
        if base.endswith("y"):
            out.add(base[:-1] + "ies")
        if base.endswith("s"):
            out.add(base[:-1])
    return out


def lister_op_noun(op: str) -> str:
    """Trailing noun of a lister op, lowercased (``DescribeVpcs`` → ``vpcs``); project() matches."""
    return re.sub(r"^(Describe|List|Get)", "", op).lower()
