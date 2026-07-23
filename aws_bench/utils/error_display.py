"""Render a caught exception to the console for CLI commands.

One helper, shared by ``aws-bench run`` and the ``env`` commands, so every
command surfaces failures the same way: a tidy message by default, a full
traceback under ``--debug``.
"""

from __future__ import annotations

import boto3
import botocore
from rich.console import Console


def print_exception(console: Console, *, debug: bool) -> None:
    """Render the exception being handled to ``console``.

    Default (``debug=False``): suppress the traceback frames and print just the
    ``ExcType: message`` (plus any chained cause). Under ``debug=True``: print the
    full Rich traceback with locals, but collapse the boto3/botocore frames (deep
    credential/HTTP machinery that rarely points at the real cause).

    Must be called from within an ``except`` block — it reads the active
    exception via ``sys.exc_info()``.
    """
    if debug:
        console.print_exception(show_locals=True, suppress=[boto3, botocore])
    else:
        console.print_exception(width=0)
