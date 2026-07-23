"""Quota management models."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class QuotaStatus(Enum):
    """Outcome of a quota increase request; the source of truth for quota state.

    Members fall into three groups, partitioned by the :attr:`is_pending`,
    :attr:`is_success`, and :attr:`is_failure` predicates (exactly one is true
    for any member):

    - pending: ``REQUESTED``, ``ALREADY_PENDING`` — submitted, awaiting AWS.
    - success: ``ALREADY_MET``, ``APPROVED``, ``CASE_CLOSED`` — granted.
      ``CASE_CLOSED`` mirrors the AWS status of the same name (support case
      closed, typically approved); the new value may not have propagated yet,
      so re-check the effective value rather than treating it as a failure.
    - failure: ``DENIED``, ``FAILED`` — rejected or errored.

    :meth:`from_aws_status` maps raw AWS Service Quotas wire statuses onto
    these members.
    """

    REQUESTED = "REQUESTED"
    ALREADY_PENDING = "ALREADY_PENDING"
    ALREADY_MET = "ALREADY_MET"
    APPROVED = "APPROVED"
    CASE_CLOSED = "CASE_CLOSED"
    DENIED = "DENIED"
    FAILED = "FAILED"

    @property
    def is_pending(self) -> bool:
        """True iff the request is submitted but not yet resolved by AWS."""
        return self in _PENDING_STATUSES

    @property
    def is_success(self) -> bool:
        """True iff the quota is granted (met, approved, or case-closed).

        ``CASE_CLOSED`` is a success even though the effective value may still
        be propagating — the increase itself was granted.
        """
        return self in _SUCCESS_STATUSES

    @property
    def is_failure(self) -> bool:
        """True iff the request was rejected (``DENIED``) or errored (``FAILED``)."""
        return self in _FAILURE_STATUSES

    @classmethod
    def from_aws_status(cls, aws_status: str) -> QuotaStatus:
        """Map an AWS Service Quotas request status string onto a QuotaStatus.

        AWS reports several wire statuses that collapse onto our members:
        ``PENDING``/``CASE_OPENED`` are pending; ``DENIED``/``NOT_APPROVED``
        are denied; ``APPROVED`` and ``CASE_CLOSED`` are granted. Any other
        value (``INVALID_REQUEST``, empty, or an unrecognized future status)
        is treated as ``FAILED`` rather than assumed successful.
        """
        if aws_status in _AWS_PENDING_STATUSES:
            return cls.ALREADY_PENDING
        if aws_status in _AWS_DENIED_STATUSES:
            return cls.DENIED
        if aws_status == "CASE_CLOSED":
            return cls.CASE_CLOSED
        if aws_status == "APPROVED":
            return cls.APPROVED
        return cls.FAILED


_PENDING_STATUSES = frozenset({QuotaStatus.REQUESTED, QuotaStatus.ALREADY_PENDING})
_SUCCESS_STATUSES = frozenset(
    {QuotaStatus.ALREADY_MET, QuotaStatus.APPROVED, QuotaStatus.CASE_CLOSED}
)
_FAILURE_STATUSES = frozenset({QuotaStatus.DENIED, QuotaStatus.FAILED})

_AWS_PENDING_STATUSES = frozenset({"PENDING", "CASE_OPENED"})
_AWS_DENIED_STATUSES = frozenset({"DENIED", "NOT_APPROVED"})


@dataclass
class QuotaIncreaseRequest:
    """A single service quota increase to request."""

    service_code: str
    quota_code: str
    desired_value: float


@dataclass
class QuotaIncreaseResult:
    """Outcome of a single quota increase request."""

    service_code: str
    quota_code: str
    desired_value: float
    status: QuotaStatus
    error_message: str = ""


@dataclass
class QuotaConfiguration:
    """Configuration for a batch of quota increase requests."""

    increases: list[QuotaIncreaseRequest]
    region: str = "us-east-1"


@dataclass
class QuotaEntry:
    """One requested quota's current-vs-desired state, for reporting."""

    region: str
    quota_id: str
    name: str
    requested: float
    current: float | None  # None when the current value couldn't be read
    met: bool

    @property
    def is_met(self) -> bool:
        """Whether the current value satisfies the requested increase."""
        return self.met
