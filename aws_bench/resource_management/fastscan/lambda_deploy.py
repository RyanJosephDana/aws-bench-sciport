"""Idempotent deploy of the fast-scan discovery Lambda + its IAM role.

Runs against the management account, called by provisioning before a run when the
Lambda scan backend is selected. The deploy is NOT best-effort: the Lambda scan is
the point (the host scan is what broke at scale), so a failed deploy propagates and
halts the run rather than letting it proceed to a scan that would fail later.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import tenacity
from botocore.exceptions import ClientError

from aws_bench.account_management.constants import ORG_ACCESS_ROLE
from aws_bench.logging.logger import get_logger
from aws_bench.resource_management.fastscan.constants import (
    LAMBDA_FUNCTION_NAME,
    LAMBDA_FUNCTION_TIMEOUT_S,
    LAMBDA_HANDLER,
    LAMBDA_MEMORY_MB,
    LAMBDA_ROLE_NAME,
    LAMBDA_RUNTIME,
)
from aws_bench.utils.concurrent import build_client
from aws_bench.utils.credentials_provider import CredentialProvider

logger = get_logger(__name__)

# The execution role needs only CloudWatch Logs (basic exec) + the inline cross-account assume
# below. The scan's list/describe calls run on the assumed member-account session (the org access
# role), not this role, so no scan permissions are attached here. The assume is unscoped (no
# session policy), matching the host scan path.
_BASIC_EXEC_ARN = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"

# A just-created role is not yet visible to Lambda's control plane (IAM is eventually-consistent),
# so the first create_function can be rejected with InvalidParameterValueException ("the role
# cannot be assumed by Lambda"). Retried below with backoff until propagation converges.
_CREATE_ROLE_RETRY_ATTEMPTS = 5

# Concurrent management-account creations deploy the same Lambda; only one update is allowed in
# flight, so losers collide with ResourceConflictException. Retried below until the slot frees.
# More attempts than the create retry since up to N deploys serialize through the one update slot.
_UPDATE_CONFLICT_RETRY_ATTEMPTS = 8


def _is_role_not_yet_assumable(exc: BaseException) -> bool:
    """True for the transient ``create_function`` rejection while the new role propagates.

    Matched by botocore error code, not the client's exception type, because that type is a
    dynamic client attribute (no importable class to give ``retry_if_exception_type``).
    """
    return (
        isinstance(exc, ClientError)
        and exc.response.get("Error", {}).get("Code") == "InvalidParameterValueException"
    )


def _is_concurrent_update_conflict(exc: BaseException) -> bool:
    """True for the ResourceConflictException a colliding concurrent update raises.

    A function holds only one update in flight, so a losing deploy's ``update_function_*`` (or one
    issued while the winner's create still leaves it ``Pending``) is rejected with "an update is in
    progress" — transient, retried until the slot frees. Matched by error code (the client type is
    a dynamic attribute, as above). The "function already exists" conflict comes only from
    ``create_function``, which ``ensure_deployed`` routes to the update path, not here.
    """
    return (
        isinstance(exc, ClientError)
        and exc.response.get("Error", {}).get("Code") == "ResourceConflictException"
    )


def _clients() -> tuple[object, object]:
    """Return (lambda_client, iam_client) built from the management session.

    Client construction funnels through ``build_client`` per the process-wide
    invariant (concurrent boto3 client builds race OpenSSL C state → SIGSEGV).
    """
    mgmt = CredentialProvider.get().get_management_session()
    return build_client(mgmt, "lambda"), build_client(mgmt, "iam")


def _assume_role_policy() -> str:
    return json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "lambda.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                }
            ],
        }
    )


def _cross_account_assume_policy() -> str:
    # Grants the discovery Lambda the one privileged action it needs: assume the org access
    # role in member accounts. The list/describe calls then run on those assumed (unscoped)
    # member-account credentials, not on this execution role.
    return json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": "sts:AssumeRole",
                    "Resource": f"arn:aws:iam::*:role/{ORG_ACCESS_ROLE}",
                }
            ],
        }
    )


# The handler's exact import closure, packaged verbatim: importing lambda_handler pulls in
# precisely these modules. An explicit manifest keeps out host-only modules that import deps
# absent from the runtime (utils/retry→tenacity, utils/error_display→rich, logging/ledger→
# shortuuid). Adding a module not listed here fails test_lambda_import_isolation.
_CLOSURE_FILES = (
    "exceptions.py",
    "account_management/__init__.py",
    "account_management/constants.py",
    "resource_management/__init__.py",
    "resource_management/fastscan/__init__.py",
    "resource_management/fastscan/constants.py",
    "resource_management/fastscan/engine.py",
    "resource_management/fastscan/lambda_handler.py",
    "resource_management/fastscan/lambda_protocol.py",
    "resource_management/fastscan/models.py",
    "resource_management/fastscan/runtime.py",
    "resource_management/fastscan/listers/__init__.py",
    "resource_management/fastscan/listers/custom_listers.py",
    "resource_management/fastscan/listers/lister_registry.py",
    "resource_management/fastscan/listers/model.py",
    "resource_management/fastscan/listers/region_policy.py",
    "resource_management/fastscan/listers/region_skip.py",
    "resource_management/fastscan/listers/simple_listers.py",
    "utils/__init__.py",
    "utils/concurrent.py",
)

# Three entry points ship as shims so the closure resolves without the host-only deps (rich) and
# without dist-metadata:
#   aws_bench/__init__.py         real one calls importlib.metadata.version("aws-bench"); the dist
#                                 isn't installed in-Lambda. Emptied.
#   aws_bench/logging/__init__.py real one re-exports the rich-backed logger surface. Emptied;
#                                 the handler imports logging.logger directly.
#   aws_bench/logging/logger.py   replaced with a stdlib get_logger. Mirrors the real
#                                 get_logger(name, *filters); filters only tag log lines.
_SHIM_FILES = {
    "__init__.py": "",
    "logging/__init__.py": "",
    # Stdlib get_logger shim (the real one pulls rich). Raises the root to DEBUG so the scan's
    # own DEBUG lines (per-lister skips, page-cap truncation, timing) reach CloudWatch for
    # diagnosing a slow/partial scan. This is only safe because botocore/boto3 are pinned to
    # WARNING first: a logger with an explicit level is the stopping point for isEnabledFor, so
    # the DEBUG root never reaches them — their DEBUG records (SigV4 Authorization +
    # X-Amz-Security-Token for the assumed member-account session, plus every list/describe
    # response body) stay suppressed. Drop the two pins and the DEBUG root leaks the token.
    "logging/logger.py": (
        "import logging\n\n\n"
        "def get_logger(name=None, *args, **kwargs):\n"
        "    logging.getLogger('botocore').setLevel(logging.WARNING)\n"
        "    logging.getLogger('boto3').setLevel(logging.WARNING)\n"
        "    logging.getLogger().setLevel(logging.DEBUG)\n"
        "    return logging.getLogger(name or 'fastscan')\n"
    ),
}


def _build_zip() -> bytes:
    """Package exactly the fast-scan closure as a Lambda zip: real source + three shims."""
    pkg_root = Path(__file__).resolve().parents[2]  # .../aws_bench
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in _CLOSURE_FILES:
            zf.write(pkg_root / rel, f"aws_bench/{rel}")
        for rel, content in _SHIM_FILES.items():
            zf.writestr(f"aws_bench/{rel}", content)
    return buf.getvalue()


def _ensure_role(iam: object) -> str:
    """Create or reconcile the execution role to its expected shape; return its ARN.

    No propagation sleep here — the create_function retry loop absorbs the IAM
    consistency delay, waiting exactly as long as convergence actually takes.

    On an existing role, the trust policy is reset and managed-policy attachments are
    reconciled to exactly ``_BASIC_EXEC_ARN`` (detaching anything else), so the deploy owns
    the role's shape rather than trusting whatever is already there. This role can assume the
    org access role org-wide, so a manually-attached policy or a drifted trust policy is a
    privilege change the next deploy must correct, not silently keep.
    """
    try:
        existing = iam.get_role(RoleName=LAMBDA_ROLE_NAME)  # type: ignore[attr-defined]
        arn = existing["Role"]["Arn"]
        iam.update_assume_role_policy(  # type: ignore[attr-defined]
            RoleName=LAMBDA_ROLE_NAME, PolicyDocument=_assume_role_policy()
        )
        _reconcile_managed_policies(iam)
    except iam.exceptions.NoSuchEntityException:  # type: ignore[attr-defined]
        created = iam.create_role(  # type: ignore[attr-defined]
            RoleName=LAMBDA_ROLE_NAME, AssumeRolePolicyDocument=_assume_role_policy()
        )
        arn = created["Role"]["Arn"]
        iam.attach_role_policy(RoleName=LAMBDA_ROLE_NAME, PolicyArn=_BASIC_EXEC_ARN)  # type: ignore[attr-defined]
    iam.put_role_policy(  # type: ignore[attr-defined]
        RoleName=LAMBDA_ROLE_NAME,
        PolicyName="fastscan-cross-account-assume",
        PolicyDocument=_cross_account_assume_policy(),
    )
    return arn


def _reconcile_managed_policies(iam: object) -> None:
    """Attach ``_BASIC_EXEC_ARN`` and detach every other managed policy on the role."""
    attached = iam.list_attached_role_policies(RoleName=LAMBDA_ROLE_NAME)  # type: ignore[attr-defined]
    current = {p["PolicyArn"] for p in attached["AttachedPolicies"]}
    if _BASIC_EXEC_ARN not in current:
        iam.attach_role_policy(RoleName=LAMBDA_ROLE_NAME, PolicyArn=_BASIC_EXEC_ARN)  # type: ignore[attr-defined]
    for policy_arn in current - {_BASIC_EXEC_ARN}:
        logger.info("Detaching unexpected managed policy from discovery role: %s", policy_arn)
        iam.detach_role_policy(RoleName=LAMBDA_ROLE_NAME, PolicyArn=policy_arn)  # type: ignore[attr-defined]


@tenacity.retry(
    retry=tenacity.retry_if_exception(_is_role_not_yet_assumable),
    wait=tenacity.wait_exponential(multiplier=2, min=2, max=20) + tenacity.wait_random(0, 2),
    stop=tenacity.stop_after_attempt(_CREATE_ROLE_RETRY_ATTEMPTS),
    reraise=True,
)
def _create_function(lam: object, role_arn: str, code: bytes) -> None:
    """Create the function, retrying while the freshly-created role is still propagating.

    ``InvalidParameterValueException`` on create is (almost always) IAM not yet having
    propagated the new role to Lambda; back off and retry rather than fail the deploy.
    """
    lam.create_function(  # type: ignore[attr-defined]
        FunctionName=LAMBDA_FUNCTION_NAME,
        Runtime=LAMBDA_RUNTIME,
        Role=role_arn,
        Handler=LAMBDA_HANDLER,
        Code={"ZipFile": code},
        Timeout=LAMBDA_FUNCTION_TIMEOUT_S,
        MemorySize=LAMBDA_MEMORY_MB,
    )


@tenacity.retry(
    retry=tenacity.retry_if_exception(_is_concurrent_update_conflict),
    wait=tenacity.wait_exponential(multiplier=2, min=2, max=20) + tenacity.wait_random(0, 2),
    stop=tenacity.stop_after_attempt(_UPDATE_CONFLICT_RETRY_ATTEMPTS),
    reraise=True,
)
def _update_function(lam: object, role_arn: str, code: bytes) -> None:
    """Reconcile an existing function's configuration and code, waiting between each.

    Updating code alone would leave a changed timeout/memory/runtime/handler/role silently
    unapplied. Config and code are separate update calls, and a function can hold only one
    update in flight, so each is followed by the updated-waiter before the next proceeds
    (and before the same run invokes the function).

    Retried on ResourceConflictException from a colliding concurrent deploy. Both calls are
    idempotent, so replaying the whole reconcile is safe; the waiter between them means a conflict
    on the code update is another deployer, not our own config update still settling.
    """
    updated = lam.get_waiter("function_updated_v2")  # type: ignore[attr-defined]
    lam.update_function_configuration(  # type: ignore[attr-defined]
        FunctionName=LAMBDA_FUNCTION_NAME,
        Role=role_arn,
        Handler=LAMBDA_HANDLER,
        Runtime=LAMBDA_RUNTIME,
        Timeout=LAMBDA_FUNCTION_TIMEOUT_S,
        MemorySize=LAMBDA_MEMORY_MB,
    )
    updated.wait(FunctionName=LAMBDA_FUNCTION_NAME)
    lam.update_function_code(  # type: ignore[attr-defined]
        FunctionName=LAMBDA_FUNCTION_NAME, ZipFile=code
    )
    updated.wait(FunctionName=LAMBDA_FUNCTION_NAME)


def ensure_deployed() -> None:
    """Create or update the discovery Lambda + role, leaving it ready to invoke. Idempotent."""
    logger.debug(
        "Deploying fast-scan discovery Lambda %r to the management account", LAMBDA_FUNCTION_NAME
    )
    lam, iam = _clients()
    role_arn = _ensure_role(iam)
    code = _build_zip()
    try:
        _create_function(lam, role_arn, code)
        # A created function is Pending briefly; wait for Active so the same run's
        # first invoke doesn't race the cold create.
        lam.get_waiter("function_active_v2").wait(  # type: ignore[attr-defined]
            FunctionName=LAMBDA_FUNCTION_NAME
        )
        logger.info("Created fast-scan discovery Lambda %r", LAMBDA_FUNCTION_NAME)
    except lam.exceptions.ResourceConflictException:  # type: ignore[attr-defined]
        try:
            _update_function(lam, role_arn, code)
            logger.info("Updated existing fast-scan discovery Lambda %r", LAMBDA_FUNCTION_NAME)
        except ClientError as exc:
            if not _is_concurrent_update_conflict(exc):
                raise  # a real update failure (AccessDenied, etc.) still halts the run
            # Conflict retry exhausted: another concurrent deploy still owns the update slot. The
            # function already exists and every concurrent deploy pushes identical code/config, so
            # the winner leaves it in the shape we wanted — proceed rather than fail env_init. A
            # still-Pending function is absorbed by the scanner's own invoke retry.
            logger.debug(
                "Fast-scan discovery Lambda %r update still conflicting after %d attempts; "
                "another concurrent deploy owns the update slot — proceeding",
                LAMBDA_FUNCTION_NAME,
                _UPDATE_CONFLICT_RETRY_ATTEMPTS,
            )
