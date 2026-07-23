"""Tests for the `env list` command."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from aws_bench.account_management.models import OrgInfo
from aws_bench.cli.main import app

ENV = "aws_bench.cli.env"
runner = CliRunner()


def _make_org_info():
    return OrgInfo(
        org_id="o-abc123",
        root_id="r-root1",
        management_account_id="111111111111",
        management_account_email="mgmt@example.com",
    )


def _mock_preflight(*args, **kwargs):
    return "111111111111"


class TestEnvList:
    def test_list_shows_environments(self):
        """List displays a table of test environments with account counts."""
        ous = [
            {"Id": "ou-xxxx-xxxxxxxx", "Name": "dev-bench", "Arn": "arn:aws:..."},
            {"Id": "ou-yyyy-yyyyyyyy", "Name": "staging", "Arn": "arn:aws:..."},
        ]
        dev_accounts = [
            {"Id": "222222222222", "Name": "acct-1", "Status": "ACTIVE"},
            {"Id": "333333333333", "Name": "acct-2", "Status": "ACTIVE"},
            {"Id": "444444444444", "Name": "acct-3", "Status": "SUSPENDED"},
        ]
        staging_accounts = [
            {"Id": "555555555555", "Name": "acct-4", "Status": "ACTIVE"},
        ]

        with (
            patch(f"{ENV}.OrganizationsClient") as mock_org_cls,
            patch(f"{ENV}.CredentialProvider") as mock_cred_cls,
            patch(f"{ENV}.preflight_aws_credentials", side_effect=_mock_preflight),
        ):
            mock_org = MagicMock()
            mock_org_cls.return_value = mock_org
            mock_org.get_org_info.return_value = _make_org_info()
            mock_org.list_ous.return_value = ous
            mock_org.list_accounts_in_ou.side_effect = lambda ou_id: {
                "ou-xxxx-xxxxxxxx": dev_accounts,
                "ou-yyyy-yyyyyyyy": staging_accounts,
            }[ou_id]

            mock_cred_cls.get.return_value = MagicMock()

            result = runner.invoke(app, ["env", "list"])

        assert result.exit_code == 0
        assert "dev-bench" in result.stdout
        assert "staging" in result.stdout
        assert "ou-xxxx-xxxxxxxx" in result.stdout
        assert "ou-yyyy-yyyyyyyy" in result.stdout
        # dev-bench has 2 active accounts (one is SUSPENDED)
        assert "2" in result.stdout
        # staging has 1 active account
        assert "1" in result.stdout

    def test_list_no_environments(self):
        """List prints a message when no OUs exist."""
        with (
            patch(f"{ENV}.OrganizationsClient") as mock_org_cls,
            patch(f"{ENV}.CredentialProvider") as mock_cred_cls,
            patch(f"{ENV}.preflight_aws_credentials", side_effect=_mock_preflight),
        ):
            mock_org = MagicMock()
            mock_org_cls.return_value = mock_org
            mock_org.get_org_info.return_value = _make_org_info()
            mock_org.list_ous.return_value = []

            mock_cred_cls.get.return_value = MagicMock()

            result = runner.invoke(app, ["env", "list"])

        assert result.exit_code == 0
        assert "No test environments found" in result.stdout

    def test_list_credential_failure(self):
        """Exits 1 when credentials are invalid."""
        from aws_bench.cli.preflight import PreflightError

        with (
            patch(f"{ENV}.CredentialProvider") as mock_cred_cls,
            patch(
                f"{ENV}.preflight_aws_credentials",
                side_effect=PreflightError("AWS credentials are missing or expired"),
            ),
        ):
            mock_cred_cls.get.return_value = MagicMock()

            result = runner.invoke(app, ["env", "list"])

        assert result.exit_code == 1

    def test_list_partial_failure_shows_question_mark(self):
        """When one OU's account listing fails, its count shows as '?' and exit is still 0."""
        ous = [
            {"Id": "ou-xxxx-xxxxxxxx", "Name": "healthy", "Arn": "arn:aws:..."},
            {"Id": "ou-yyyy-yyyyyyyy", "Name": "broken", "Arn": "arn:aws:..."},
        ]
        healthy_accounts = [
            {"Id": "222222222222", "Name": "acct-1", "Status": "ACTIVE"},
        ]

        with (
            patch(f"{ENV}.OrganizationsClient") as mock_org_cls,
            patch(f"{ENV}.CredentialProvider") as mock_cred_cls,
            patch(f"{ENV}.preflight_aws_credentials", side_effect=_mock_preflight),
        ):
            mock_org = MagicMock()
            mock_org_cls.return_value = mock_org
            mock_org.get_org_info.return_value = _make_org_info()
            mock_org.list_ous.return_value = ous

            def _accounts_side_effect(ou_id):
                if ou_id == "ou-xxxx-xxxxxxxx":
                    return healthy_accounts
                raise Exception("Throttled")

            mock_org.list_accounts_in_ou.side_effect = _accounts_side_effect

            mock_cred_cls.get.return_value = MagicMock()

            result = runner.invoke(app, ["env", "list"])

        assert result.exit_code == 0
        assert "healthy" in result.stdout
        assert "broken" in result.stdout
        assert "1" in result.stdout
        # The '?' appears in the rendered output (Rich markup stripped by CliRunner)
        assert "?" in result.stdout
