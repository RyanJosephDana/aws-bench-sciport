"""CLI test configuration.

Unset FORCE_COLOR so Rich does not emit ANSI escape codes when output is
captured by pytest's capsys/CliRunner — assertions check plaintext content.
The module-level TeeConsole is also patched since it was created before the
fixture could affect the env.
"""

import pytest

from aws_bench.cli.ui import TeeConsole


@pytest.fixture(autouse=True)
def _no_force_color(monkeypatch):
    """Prevent Rich from forcing ANSI output in tests.

    The module-level ``console`` was instantiated at import time (while
    FORCE_COLOR was set), so patching the env alone is not enough. Replace
    every module's reference to the console with a no-color instance.
    """
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    plain_console = TeeConsole(no_color=True)
    monkeypatch.setattr("aws_bench.cli.display.console", plain_console)
    monkeypatch.setattr("aws_bench.cli.ui.console", plain_console)
    monkeypatch.setattr("aws_bench.cli.jobs.console", plain_console)
    monkeypatch.setattr("aws_bench.cli.env.console", plain_console)
