"""``aws-bench view`` CLI — browse job results and task definitions."""

from pathlib import Path
from typing import Annotated

from harbor.cli.view import view_command
from typer import Argument, Option


def view(
    folder: Annotated[
        Path,
        Argument(help="Folder containing job results or task definitions."),
    ],
    port: Annotated[
        str,
        Option("-p", "--port", help="Port or port range to bind to."),
    ] = "8080-8089",
    host: Annotated[
        str,
        Option("--host", help="Host address to bind to."),
    ] = "127.0.0.1",
    tasks: Annotated[
        bool,
        Option("--tasks", help="Force task-definitions browsing mode."),
    ] = False,
    jobs: Annotated[
        bool,
        Option("--jobs", help="Force job-results browsing mode."),
    ] = False,
) -> None:
    """Start web server to browse trajectory files."""
    view_command(
        folder=folder,
        port=port,
        host=host,
        tasks=tasks,
        jobs=jobs,
    )
