"""Tests for SCP region-deny detection in trajectory metrics."""

from __future__ import annotations

from unittest.mock import MagicMock

from harbor.models.trajectories import Trajectory

from aws_bench.metrics.aggregation import aggregate_detailed
from aws_bench.metrics.run_data import (
    TrialData,
    _contains_scp_deny,
    _scp_deny_count_from_trajectory,
)

# -- Helpers ------------------------------------------------------------------

SCP_ERROR_S3 = (
    "An error occurred (AccessDenied) when calling the ListBuckets operation: "
    "User: arn:aws:sts::123456789012:assumed-role/TestRole/session is not authorized "
    "to perform: s3:ListBuckets on resource: arn:aws:s3:::* with an explicit deny "
    "in a service control policy"
)

SCP_ERROR_EC2 = (
    "An error occurred (UnauthorizedOperation) when calling the RunInstances operation: "
    "You are not authorized to perform this operation. User: "
    "arn:aws:sts::123456789012:assumed-role/TestRole/session is not authorized to perform: "
    "ec2:RunInstances on resource: arn:aws:ec2:eu-west-1:123456789012:instance/* "
    "with an explicit deny in a service control policy"
)

SCP_ERROR_LAMBDA = (
    "An error occurred (AccessDeniedException) when calling the Invoke operation: "
    "User: arn:aws:sts::123456789012:assumed-role/TestRole/session is not authorized "
    "to perform: lambda:InvokeFunction on resource: "
    "arn:aws:lambda:ap-southeast-1:123456789012:function:my-func "
    "with an explicit deny in a service control policy"
)

IAM_DENY_ERROR = (
    "An error occurred (AccessDenied) when calling the PutObject operation: "
    "User: arn:aws:sts::123456789012:assumed-role/TestRole/session is not authorized "
    "to perform: s3:PutObject on resource: arn:aws:s3:::my-bucket/key "
    "with an explicit deny in an identity-based policy"
)

GENERIC_ERROR = "Something went wrong with the API call."


_AGENT = {"name": "test-agent", "version": "1.0"}


def _build_trajectory(observation_contents: list[str | list[dict] | None]) -> Trajectory:
    """Build a minimal trajectory with given observation result contents."""
    steps = []
    for i, content in enumerate(observation_contents):
        step_data: dict = {
            "step_id": i + 1,
            "source": "agent",
            "message": "doing stuff",
            "observation": {
                "results": [{"content": content}],
            },
        }
        steps.append(step_data)
    return Trajectory.model_validate(
        {
            "schema_version": "ATIF-v1.7",
            "agent": _AGENT,
            "steps": steps,
        }
    )


# -- Tests --------------------------------------------------------------------


class TestScpDenyCountFromTrajectory:
    """Tests for _scp_deny_count_from_trajectory."""

    def test_none_trajectory_returns_zero(self) -> None:
        assert _scp_deny_count_from_trajectory(None) == 0

    def test_no_observations_returns_zero(self) -> None:
        traj = Trajectory.model_validate(
            {
                "schema_version": "ATIF-v1.7",
                "agent": _AGENT,
                "steps": [{"step_id": 1, "source": "agent", "message": "hi"}],
            }
        )
        assert _scp_deny_count_from_trajectory(traj) == 0

    def test_null_content_returns_zero(self) -> None:
        traj = _build_trajectory([None])
        assert _scp_deny_count_from_trajectory(traj) == 0

    def test_no_scp_error_returns_zero(self) -> None:
        traj = _build_trajectory([GENERIC_ERROR, IAM_DENY_ERROR])
        assert _scp_deny_count_from_trajectory(traj) == 0

    def test_detects_s3_scp_deny(self) -> None:
        traj = _build_trajectory([SCP_ERROR_S3])
        assert _scp_deny_count_from_trajectory(traj) == 1

    def test_detects_ec2_scp_deny(self) -> None:
        traj = _build_trajectory([SCP_ERROR_EC2])
        assert _scp_deny_count_from_trajectory(traj) == 1

    def test_detects_lambda_scp_deny(self) -> None:
        traj = _build_trajectory([SCP_ERROR_LAMBDA])
        assert _scp_deny_count_from_trajectory(traj) == 1

    def test_counts_multiple_scp_denies(self) -> None:
        traj = _build_trajectory([SCP_ERROR_S3, GENERIC_ERROR, SCP_ERROR_EC2, SCP_ERROR_LAMBDA])
        assert _scp_deny_count_from_trajectory(traj) == 3

    def test_does_not_count_iam_deny(self) -> None:
        traj = _build_trajectory([IAM_DENY_ERROR])
        assert _scp_deny_count_from_trajectory(traj) == 0

    def test_multimodal_content_text_part(self) -> None:
        """SCP error in a ContentPart list is detected."""
        content_parts = [{"type": "text", "text": SCP_ERROR_S3}]
        traj = _build_trajectory([content_parts])
        assert _scp_deny_count_from_trajectory(traj) == 1

    def test_multimodal_content_no_match(self) -> None:
        content_parts = [{"type": "text", "text": "all good"}]
        traj = _build_trajectory([content_parts])
        assert _scp_deny_count_from_trajectory(traj) == 0

    def test_multimodal_content_image_only(self) -> None:
        """Image-only content parts don't crash or match."""
        content_parts = [{"type": "image", "source": {"media_type": "image/png", "path": "x.png"}}]
        traj = _build_trajectory([content_parts])
        assert _scp_deny_count_from_trajectory(traj) == 0

    def test_multimodal_mixed_content(self) -> None:
        """Only one count per observation result even with multiple text parts."""
        content_parts = [
            {"type": "text", "text": "Trying operation..."},
            {"type": "text", "text": SCP_ERROR_EC2},
        ]
        traj = _build_trajectory([content_parts])
        assert _scp_deny_count_from_trajectory(traj) == 1

    def test_multiple_results_per_observation(self) -> None:
        """Multiple results within a single step observation are each counted."""
        traj = Trajectory.model_validate(
            {
                "schema_version": "ATIF-v1.7",
                "agent": _AGENT,
                "steps": [
                    {
                        "step_id": 1,
                        "source": "agent",
                        "message": "multi-tool call",
                        "tool_calls": [
                            {
                                "tool_call_id": "tc1",
                                "function_name": "bash",
                                "arguments": {},
                            },
                            {
                                "tool_call_id": "tc2",
                                "function_name": "bash",
                                "arguments": {},
                            },
                        ],
                        "observation": {
                            "results": [
                                {"source_call_id": "tc1", "content": SCP_ERROR_S3},
                                {"source_call_id": "tc2", "content": SCP_ERROR_EC2},
                            ],
                        },
                    }
                ],
            }
        )
        assert _scp_deny_count_from_trajectory(traj) == 2


class TestScpDenyAggregation:
    """Tests for SCP deny metrics in aggregation."""

    def _make_trial_data(self, observation_contents: list[str | list[dict] | None]) -> TrialData:
        traj = _build_trajectory(observation_contents) if observation_contents else None
        result = MagicMock()
        result.verifier_result = None
        result.exception_info = None
        result.agent_execution = None
        result.started_at = None
        result.finished_at = None
        result.task_name = "test-task"
        result.trial_name = "trial-1"
        result.compute_token_cost_totals.return_value = (None, None, None, None)
        return TrialData(
            trial_dir=MagicMock(),
            result=result,
            trajectory=traj,
            raw_result=None,
        )

    def test_trial_data_scp_deny_count_property(self) -> None:
        td = self._make_trial_data([SCP_ERROR_S3, GENERIC_ERROR, SCP_ERROR_EC2])
        assert td.scp_deny_count == 2

    def test_trial_data_scp_deny_count_zero(self) -> None:
        td = self._make_trial_data([GENERIC_ERROR])
        assert td.scp_deny_count == 0

    def test_trial_data_no_trajectory(self) -> None:
        td = self._make_trial_data([])
        # Empty list builds a trajectory with no observation content
        assert td.scp_deny_count == 0

    def test_aggregate_detailed_includes_scp_metrics(self) -> None:
        trials = [
            self._make_trial_data([SCP_ERROR_S3, SCP_ERROR_EC2]),
            self._make_trial_data([GENERIC_ERROR]),
            self._make_trial_data([SCP_ERROR_LAMBDA]),
        ]
        metrics = aggregate_detailed(trials)
        assert metrics["n_scp_deny_total"] == 3
        assert metrics["n_trials_with_scp_deny"] == 2

    def test_aggregate_detailed_zero_scp_denies(self) -> None:
        trials = [
            self._make_trial_data([GENERIC_ERROR]),
            self._make_trial_data([IAM_DENY_ERROR]),
        ]
        metrics = aggregate_detailed(trials)
        assert metrics["n_scp_deny_total"] == 0
        assert metrics["n_trials_with_scp_deny"] == 0


class TestContainsScpDeny:
    """Tests for _contains_scp_deny ARN regex and fallback matching."""

    # Real error messages captured from live SCP test
    REAL_EC2 = (
        "An error occurred (UnauthorizedOperation) when calling the DescribeInstances operation: "
        "You are not authorized to perform this operation. User: "
        "arn:aws:sts::123456789012:assumed-role/OrganizationAccountAccessRole/scp-test "
        "is not authorized to perform: ec2:DescribeInstances with an explicit deny in a "
        "service control policy: arn:aws:organizations::123:policy/o-f2ullgv2hg/"
        "service_control_policy/p-qprcwfer"
    )

    REAL_S3 = (
        "An error occurred (AccessDenied) when calling the ListBuckets operation: "
        "User: arn:aws:sts::123456789012:assumed-role/OrganizationAccountAccessRole/scp-test "
        "is not authorized to perform: s3:ListAllMyBuckets with an explicit deny in a "
        "service control policy: arn:aws:organizations::123:policy/o-f2ullgv2hg/"
        "service_control_policy/p-qprcwfer"
    )

    REAL_LAMBDA = (
        "An error occurred (AccessDeniedException) when calling the ListFunctions operation: "
        "User: arn:aws:sts::123456789012:assumed-role/OrganizationAccountAccessRole/scp-test "
        "is not authorized to perform: lambda:ListFunctions on resource: * with an explicit "
        "deny in a service control policy: arn:aws:organizations::123:policy/"
        "o-f2ullgv2hg/service_control_policy/p-qprcwfer"
    )

    # Older format without ARN (pre-2026 enhanced messages)
    LEGACY_FORMAT = (
        "User: arn:aws:sts::123456789012:assumed-role/Role/session is not authorized "
        "to perform: ec2:RunInstances with an explicit deny in a service control policy"
    )

    def test_matches_real_ec2_error_with_arn(self) -> None:
        assert _contains_scp_deny(self.REAL_EC2) is True

    def test_matches_real_s3_error_with_arn(self) -> None:
        assert _contains_scp_deny(self.REAL_S3) is True

    def test_matches_real_lambda_error_with_arn(self) -> None:
        assert _contains_scp_deny(self.REAL_LAMBDA) is True

    def test_matches_legacy_format_without_arn(self) -> None:
        assert _contains_scp_deny(self.LEGACY_FORMAT) is True

    def test_rejects_iam_policy_deny(self) -> None:
        assert _contains_scp_deny(IAM_DENY_ERROR) is False

    def test_rejects_generic_error(self) -> None:
        assert _contains_scp_deny(GENERIC_ERROR) is False

    def test_rejects_partial_match(self) -> None:
        # Contains "service control policy" but not the full phrase
        assert _contains_scp_deny("blocked by service control policy somewhere") is False

    def test_rejects_empty_string(self) -> None:
        assert _contains_scp_deny("") is False
