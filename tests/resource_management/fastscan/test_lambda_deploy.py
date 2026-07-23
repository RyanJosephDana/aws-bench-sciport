"""Tests for the fast-scan Lambda deploy helper (zip packaging + idempotency)."""

from __future__ import annotations

import io
import zipfile
from unittest.mock import MagicMock

import pytest
import tenacity
from botocore.exceptions import ClientError

from aws_bench.resource_management.fastscan import lambda_deploy as dep


def _role_not_assumable_error() -> ClientError:
    """The botocore error create_function raises while the new role is still propagating."""
    return ClientError(
        {"Error": {"Code": "InvalidParameterValueException", "Message": "cannot be assumed"}},
        "CreateFunction",
    )


def _update_conflict_error() -> ClientError:
    """The botocore error an update call raises while another deploy holds the update slot."""
    return ClientError(
        {"Error": {"Code": "ResourceConflictException", "Message": "an update is in progress"}},
        "UpdateFunctionConfiguration",
    )


def _lam_mock() -> MagicMock:
    """A lambda-client mock with the exception classes the deploy path references."""
    lam = MagicMock()
    lam.exceptions.ResourceConflictException = type("ResourceConflictException", (Exception,), {})
    return lam


@pytest.fixture(autouse=True)
def _instant_backoff(mocker):
    """Neutralize the deploy's tenacity backoffs (create + update) so retry tests don't sleep."""
    for fn in (dep._create_function, dep._update_function):
        mocker.patch.object(fn.retry, "wait", tenacity.wait_none())  # type: ignore[attr-defined]


def _iam_mock(*, role_exists: bool, attached: list[str] | None = None) -> MagicMock:
    """An iam-client mock; ``role_exists`` toggles get_role between hit and NoSuchEntity.

    ``attached`` seeds list_attached_role_policies for the reconcile path (existing role).
    """
    iam = MagicMock()
    iam.exceptions.NoSuchEntityException = type("NoSuchEntityException", (Exception,), {})
    arn = {"Role": {"Arn": "arn:aws:iam::111111111111:role/x"}}
    if role_exists:
        iam.get_role.return_value = arn
    else:
        iam.get_role.side_effect = iam.exceptions.NoSuchEntityException()
        iam.create_role.return_value = arn
    iam.list_attached_role_policies.return_value = {
        "AttachedPolicies": [{"PolicyArn": a} for a in (attached or [])]
    }
    return iam


def test_build_zip_contains_handler_module():
    data = dep._build_zip()
    assert data  # non-empty
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
    assert any(n.endswith("fastscan/lambda_handler.py") for n in names)


def test_build_zip_ships_region_skip_module():
    """region_skip.py must ship in the closure: the handler imports it (via engine) at cold start.

    Omitting it kills every scan with a non-retryable LambdaScanFatal (there is no host fallback);
    this check catches that at build time instead.
    """
    with zipfile.ZipFile(io.BytesIO(dep._build_zip())) as zf:
        names = zf.namelist()
    assert any(n.endswith("fastscan/listers/region_skip.py") for n in names)


def test_shim_logger_enables_app_debug_but_keeps_sdk_loggers_quiet():
    """The in-Lambda logger shim raises the root to DEBUG yet suppresses the SDK loggers.

    Security-load-bearing: app DEBUG lines aid diagnosis in CloudWatch, but botocore/boto3 at
    DEBUG would log the SigV4 Authorization + X-Amz-Security-Token (the assumed member-account
    session token) and every list/describe body. The shim pins those to WARNING first, so the
    DEBUG root never reaches them (an explicit level stops isEnabledFor). Reordering or dropping
    a setLevel — which would leak the token — must fail here.
    """
    import logging
    import types

    src = dep._SHIM_FILES["logging/logger.py"]
    module = types.ModuleType("shim_logger")
    exec(compile(src, "<shim>", "exec"), module.__dict__)  # noqa: S102 — deploy-owned literal

    # Start from a non-DEBUG root so the assertion proves the shim raised it, not the ambient state.
    logging.getLogger().setLevel(logging.WARNING)
    try:
        app_logger = module.get_logger("fastscan")

        assert app_logger.isEnabledFor(logging.DEBUG)  # scan's own DEBUG lines reach CloudWatch
        assert not logging.getLogger("botocore").isEnabledFor(logging.DEBUG)  # token stays hidden
        assert not logging.getLogger("boto3").isEnabledFor(logging.DEBUG)
    finally:
        # Don't leave a DEBUG root behind for sibling tests in the same process.
        logging.getLogger().setLevel(logging.WARNING)
        logging.getLogger("botocore").setLevel(logging.NOTSET)
        logging.getLogger("boto3").setLevel(logging.NOTSET)


def test_ensure_deployed_creates_when_absent(mocker):
    lam = _lam_mock()
    iam = _iam_mock(role_exists=False)
    mocker.patch.object(dep, "_clients", return_value=(lam, iam))
    mocker.patch.object(dep, "_build_zip", return_value=b"zip")

    dep.ensure_deployed()

    iam.create_role.assert_called_once()
    lam.create_function.assert_called_once()
    # Created functions must be waited to Active before the same run invokes them.
    lam.get_waiter.assert_called_once_with("function_active_v2")
    lam.get_waiter.return_value.wait.assert_called_once()
    # Nothing to update on the create path.
    lam.update_function_code.assert_not_called()
    lam.update_function_configuration.assert_not_called()


def test_ensure_deployed_updates_code_and_config_when_function_exists(mocker):
    lam = _lam_mock()
    lam.create_function.side_effect = lam.exceptions.ResourceConflictException()
    iam = _iam_mock(role_exists=True)
    mocker.patch.object(dep, "_clients", return_value=(lam, iam))
    mocker.patch.object(dep, "_build_zip", return_value=b"zip")

    dep.ensure_deployed()

    # A re-run must reconcile BOTH the code and the configuration (timeout/memory/
    # runtime/handler/role) — update_function_code alone would silently ignore a
    # changed timeout/memory constant.
    lam.update_function_configuration.assert_called_once()
    lam.update_function_code.assert_called_once()
    cfg = lam.update_function_configuration.call_args.kwargs
    assert cfg["Timeout"] == dep.LAMBDA_FUNCTION_TIMEOUT_S
    assert cfg["MemorySize"] == dep.LAMBDA_MEMORY_MB
    assert cfg["Runtime"] == dep.LAMBDA_RUNTIME
    assert cfg["Handler"] == dep.LAMBDA_HANDLER


def test_ensure_deployed_reconciles_existing_role(mocker):
    """An existing role has its trust policy reset and unexpected managed policies detached.

    The role can assume the org access role org-wide, so the deploy must own its shape: an
    extra managed policy or a drifted trust policy is a privilege change to correct, not keep.
    """
    lam = _lam_mock()
    lam.create_function.side_effect = lam.exceptions.ResourceConflictException()
    # Role exists carrying an unexpected extra managed policy alongside basic-exec.
    iam = _iam_mock(
        role_exists=True,
        attached=[dep._BASIC_EXEC_ARN, "arn:aws:iam::aws:policy/ReadOnlyAccess"],
    )
    mocker.patch.object(dep, "_clients", return_value=(lam, iam))
    mocker.patch.object(dep, "_build_zip", return_value=b"zip")

    dep.ensure_deployed()

    # Trust policy is reset (not left as whatever the role drifted to).
    iam.update_assume_role_policy.assert_called_once()
    # The unexpected policy is detached; basic-exec (already attached) is not re-attached.
    iam.detach_role_policy.assert_called_once_with(
        RoleName=dep.LAMBDA_ROLE_NAME, PolicyArn="arn:aws:iam::aws:policy/ReadOnlyAccess"
    )
    iam.create_role.assert_not_called()


def test_ensure_deployed_waits_for_update_to_finish(mocker):
    lam = _lam_mock()
    lam.create_function.side_effect = lam.exceptions.ResourceConflictException()
    iam = _iam_mock(role_exists=True)
    mocker.patch.object(dep, "_clients", return_value=(lam, iam))
    mocker.patch.object(dep, "_build_zip", return_value=b"zip")

    dep.ensure_deployed()

    # Both updates settle via the updated-waiter (config change and code change
    # cannot be in flight simultaneously); the same run then invokes the function.
    # One waiter object, awaited twice — once after the config update, once after code.
    lam.get_waiter.assert_called_once_with("function_updated_v2")
    assert lam.get_waiter.return_value.wait.call_count == 2


def test_ensure_deployed_update_orders_config_wait_code_wait(mocker):
    """The update path fires config → wait → code → wait, in that order.

    A function holds only one update in flight, so updating code before the config
    update's waiter settles raises ResourceConflictException at deploy. Asserting the
    interleave (not just the call counts) locks the ordering the runtime requires.
    """
    lam = _lam_mock()
    lam.create_function.side_effect = lam.exceptions.ResourceConflictException()
    iam = _iam_mock(role_exists=True)
    mocker.patch.object(dep, "_clients", return_value=(lam, iam))
    mocker.patch.object(dep, "_build_zip", return_value=b"zip")

    dep.ensure_deployed()

    # All calls land on the one lam mock; filter to the update sequence and assert order.
    names = [
        c[0]
        for c in lam.mock_calls
        if c[0]
        in {
            "update_function_configuration",
            "update_function_code",
            "get_waiter().wait",
        }
    ]
    assert names == [
        "update_function_configuration",
        "get_waiter().wait",
        "update_function_code",
        "get_waiter().wait",
    ]


def test_ensure_deployed_retries_create_until_role_propagates(mocker):
    lam = _lam_mock()
    # First create attempt fails because the freshly-created role has not yet
    # propagated to Lambda's control plane; the second attempt succeeds.
    lam.create_function.side_effect = [_role_not_assumable_error(), None]
    iam = _iam_mock(role_exists=False)
    mocker.patch.object(dep, "_clients", return_value=(lam, iam))
    mocker.patch.object(dep, "_build_zip", return_value=b"zip")

    dep.ensure_deployed()

    assert lam.create_function.call_count == 2
    lam.get_waiter.assert_called_once_with("function_active_v2")


def test_ensure_deployed_gives_up_create_after_max_attempts(mocker):
    lam = _lam_mock()
    lam.create_function.side_effect = _role_not_assumable_error()
    iam = _iam_mock(role_exists=False)
    mocker.patch.object(dep, "_clients", return_value=(lam, iam))
    mocker.patch.object(dep, "_build_zip", return_value=b"zip")

    with pytest.raises(ClientError):
        dep.ensure_deployed()

    assert lam.create_function.call_count == dep._CREATE_ROLE_RETRY_ATTEMPTS


def test_ensure_deployed_does_not_retry_non_propagation_create_error(mocker):
    """A create error that isn't role-propagation propagates on the first attempt.

    The retry predicate matches ONLY InvalidParameterValueException (the transient
    "role cannot be assumed yet" signal). A different ClientError — e.g. AccessDenied —
    is a real failure; retrying it would waste four backoff cycles masking a config
    problem, so it must raise immediately without a second create_function call.
    """
    lam = _lam_mock()
    lam.create_function.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "not authorized"}},
        "CreateFunction",
    )
    iam = _iam_mock(role_exists=False)
    mocker.patch.object(dep, "_clients", return_value=(lam, iam))
    mocker.patch.object(dep, "_build_zip", return_value=b"zip")

    with pytest.raises(ClientError, match="not authorized"):
        dep.ensure_deployed()

    assert lam.create_function.call_count == 1  # no retry on a non-propagation error


def test_ensure_deployed_retries_update_on_concurrent_conflict(mocker):
    """A loser's update, rejected with ResourceConflictException, backs off and retries."""
    lam = _lam_mock()
    # Route to the update path, then conflict once on the config update before it settles.
    lam.create_function.side_effect = lam.exceptions.ResourceConflictException()
    lam.update_function_configuration.side_effect = [_update_conflict_error(), None]
    iam = _iam_mock(role_exists=True)
    mocker.patch.object(dep, "_clients", return_value=(lam, iam))
    mocker.patch.object(dep, "_build_zip", return_value=b"zip")

    dep.ensure_deployed()

    assert lam.update_function_configuration.call_count == 2
    lam.update_function_code.assert_called_once()


def test_ensure_deployed_swallows_update_conflict_after_max_attempts(mocker):
    """A never-settling update conflict is swallowed after the bounded attempts, not raised.

    The function already exists and concurrent deploys push identical code/config, so once the
    retry ladder is exhausted the winner has left it in the shape we wanted — env_init proceeds
    instead of failing rather than halting the run.
    """
    lam = _lam_mock()
    lam.create_function.side_effect = lam.exceptions.ResourceConflictException()
    lam.update_function_configuration.side_effect = _update_conflict_error()
    iam = _iam_mock(role_exists=True)
    mocker.patch.object(dep, "_clients", return_value=(lam, iam))
    mocker.patch.object(dep, "_build_zip", return_value=b"zip")

    dep.ensure_deployed()  # must not raise

    assert lam.update_function_configuration.call_count == dep._UPDATE_CONFLICT_RETRY_ATTEMPTS
    lam.update_function_code.assert_not_called()  # never got past the wedged config update


def test_ensure_deployed_does_not_retry_non_conflict_update_error(mocker):
    """A non-conflict update error raises at once (predicate matches only the conflict code)."""
    lam = _lam_mock()
    lam.create_function.side_effect = lam.exceptions.ResourceConflictException()
    lam.update_function_configuration.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "not authorized"}},
        "UpdateFunctionConfiguration",
    )
    iam = _iam_mock(role_exists=True)
    mocker.patch.object(dep, "_clients", return_value=(lam, iam))
    mocker.patch.object(dep, "_build_zip", return_value=b"zip")

    with pytest.raises(ClientError, match="not authorized"):
        dep.ensure_deployed()

    assert lam.update_function_configuration.call_count == 1  # no retry on a non-conflict error
