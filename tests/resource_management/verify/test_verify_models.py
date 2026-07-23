from aws_bench.resource_management.verify.models import (
    AccountVerifyResult,
    RegionVerifyResult,
    VerificationReport,
    VerifyResult,
)


def test_verify_success_creation():
    """Test VerifyResult creation for success case."""
    result = VerifyResult(success=True, reason="Account state matches baseline")

    assert result.success is True
    assert result.reason == "Account state matches baseline"
    assert result.details is None
    assert result.suggestion is None


def test_verify_failure_basic():
    """Test VerifyResult creation for failure case with basic args."""
    result = VerifyResult(
        success=False, reason="Found new resources", suggestion="Run 'aws-bench env reset'"
    )

    assert result.success is False
    assert result.reason == "Found new resources"
    assert result.suggestion == "Run 'aws-bench env reset'"
    assert result.details is None


def test_verify_failure_with_details():
    """Test VerifyResult with structured details."""
    details = {
        "AWS::EC2::Instance": [{"Identifier": "i-123"}],
        "AWS::S3::Bucket": [{"Identifier": "my-bucket"}],
    }

    result = VerifyResult(
        success=False, reason="Found 2 new resources", details=details, suggestion="Run reset"
    )

    assert result.success is False
    assert result.details == details
    assert result.details is not None
    assert len(result.details) == 2


def test_verify_failure_categorization():
    """Test categorization flags in VerifyResult."""
    result = VerifyResult(success=False, reason="Dataset mismatch", is_dataset_mismatch=True)

    assert result.is_dataset_mismatch is True
    assert result.is_script_mismatch is False


def test_verify_failure_with_new_resources():
    """Test VerifyResult with new_resources field."""
    new_res = {"AWS::IAM::Role": [{"Identifier": "MyRole"}]}

    result = VerifyResult(success=False, reason="New resources found", new_resources=new_res)

    assert result.new_resources == new_res
    assert result.new_resources is not None
    assert "AWS::IAM::Role" in result.new_resources


def test_verify_failure_with_drift_differences():
    """Test VerifyResult with drift_differences field."""
    drift_diff = {"stack1": {"baseline": [{"LogicalResourceId": "MyRole"}], "current": []}}

    result = VerifyResult(success=False, reason="Drift mismatch", drift_differences=drift_diff)

    assert result.drift_differences == drift_diff
    assert result.drift_differences is not None
    assert "stack1" in result.drift_differences


def test_verification_report_passed_all_success():
    results = [
        AccountVerifyResult(
            account_id="111111111111",
            environment_id="ec2-small",
            success=True,
            region_results=[RegionVerifyResult(region="us-east-1", success=True)],
        )
    ]
    report = VerificationReport(passed=True, env_name="prod-bench", results=results)
    assert report.passed is True
    assert report.env_name == "prod-bench"
    assert report.results[0].environment_id == "ec2-small"


def test_verification_report_holds_failures():
    results = [
        AccountVerifyResult(
            account_id="123456789012",
            environment_id="ec2-multiregion",
            success=False,
            region_results=[
                RegionVerifyResult(
                    region="us-east-1",
                    success=False,
                    error_message="3 unexpected resources",
                    suggestion="Run 'aws-bench env reset'",
                )
            ],
            error_message="3 unexpected resources",
        )
    ]
    report = VerificationReport(passed=False, env_name="prod-bench", results=results)
    assert report.passed is False
    assert report.results[0].region_results[0].suggestion == "Run 'aws-bench env reset'"
