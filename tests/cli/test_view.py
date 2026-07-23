"""Tests for the `view` command."""

from __future__ import annotations

from unittest.mock import patch

from typer.testing import CliRunner

from aws_bench.cli.main import app

runner = CliRunner()


class TestView:
    def test_view_delegates_to_harbor(self, tmp_path):
        """View command calls harbor's view_command with correct args."""
        with patch("aws_bench.cli.view.view_command") as mock_view:
            result = runner.invoke(app, ["view", str(tmp_path)])

            assert result.exit_code == 0
            mock_view.assert_called_once_with(
                folder=tmp_path,
                port="8080-8089",
                host="127.0.0.1",
                tasks=False,
                jobs=False,
            )

    def test_view_passes_custom_options(self, tmp_path):
        """View command forwards CLI options to harbor."""
        with patch("aws_bench.cli.view.view_command") as mock_view:
            result = runner.invoke(
                app,
                [
                    "view",
                    str(tmp_path),
                    "--port",
                    "9090",
                    "--host",
                    "0.0.0.0",
                    "--tasks",
                ],
            )

            assert result.exit_code == 0
            mock_view.assert_called_once_with(
                folder=tmp_path,
                port="9090",
                host="0.0.0.0",
                tasks=True,
                jobs=False,
            )

    def test_view_requires_folder_argument(self):
        """View command fails without a folder argument."""
        result = runner.invoke(app, ["view"])
        assert result.exit_code != 0
