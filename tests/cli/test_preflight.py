"""Tests for cli/preflight.py."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from aws_bench.cli.preflight import (
    PreflightError,
    preflight_aws_credentials,
    preflight_docker_cli,
    preflight_docker_daemon,
)


def test_preflight_docker_cli_succeeds_when_docker_on_path():
    with patch("aws_bench.cli.preflight.shutil.which", return_value="/usr/local/bin/docker"):
        preflight_docker_cli()  # no raise


def test_preflight_docker_cli_raises_when_docker_missing():
    with patch("aws_bench.cli.preflight.shutil.which", return_value=None):
        with pytest.raises(PreflightError, match="docker"):
            preflight_docker_cli()


def test_preflight_docker_daemon_succeeds_on_zero_exit():
    proc = MagicMock(returncode=0)
    with patch("aws_bench.cli.preflight.subprocess.run", return_value=proc):
        preflight_docker_daemon()


def test_preflight_docker_daemon_raises_on_nonzero_exit():
    proc = MagicMock(returncode=1, stderr=b"Cannot connect to the Docker daemon")
    with patch("aws_bench.cli.preflight.subprocess.run", return_value=proc):
        with pytest.raises(PreflightError, match="daemon"):
            preflight_docker_daemon()


def test_preflight_aws_credentials_succeeds_on_get_caller_identity():
    cred = MagicMock()
    cred.session.client.return_value.get_caller_identity.return_value = {
        "Account": "111",
        "Arn": "arn:aws:sts::111:assumed-role/Admin/me",
    }
    assert preflight_aws_credentials(cred) == "111"


def test_preflight_aws_credentials_logs_identity_with_ou(caplog):
    cred = MagicMock()
    cred.session.client.return_value.get_caller_identity.return_value = {
        "Account": "111",
        "Arn": "arn:aws:sts::111:assumed-role/Admin/me",
    }
    with caplog.at_level("INFO"):
        preflight_aws_credentials(cred, ou_name="my-ou")
    assert "111" in caplog.text
    assert "my-ou" in caplog.text


def test_preflight_aws_credentials_raises_on_sts_error():
    cred = MagicMock()
    cred.session.client.return_value.get_caller_identity.side_effect = Exception("expired")
    with pytest.raises(PreflightError, match="credentials"):
        preflight_aws_credentials(cred)
