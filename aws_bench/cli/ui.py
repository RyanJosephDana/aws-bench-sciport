"""Shared CLI console, teed into ``run.log``.

All user-facing output renders through the single :data:`console` (``typer`` is
for control flow only, never printing). The console tees a de-styled copy of
every line onto the ``aws_bench.ui`` logger, so the ledger's file handler
captures the terminal transcript — tables, panels, status lines — that otherwise
never reaches the logging tree. The console handler filters these records out so
nothing renders to the terminal twice.

``[..]`` in a printed string is Rich markup: interpolated runtime data (errors,
resource names) must not contain literal brackets.
"""

from __future__ import annotations

from rich.console import Console
from rich.text import Text

from aws_bench.logging.logger import UI_LOGGER_NAME, get_logger

_ui_logger = get_logger(UI_LOGGER_NAME)


def _tee(plaintext: str) -> None:
    """Record one already-printed UI line into run.log (skip pure whitespace)."""
    if plaintext.strip():
        _ui_logger.info(plaintext)


class TeeConsole(Console):
    """A Rich ``Console`` that mirrors everything it prints into ``run.log``.

    ``print`` renders to the terminal, then re-captures the same call as plaintext
    (markup and ANSI stripped) and tees it to the UI logger. ``soft_wrap`` defaults
    on for bare-string calls so a status line isn't wrapped mid-sentence at the
    detected width; Rich renderables keep their own layout (wrapping them would
    crop content), so the default is skipped when any arg isn't a ``str``.

    Auto-highlighting is disabled by default: we use explicit Rich markup for all
    formatting and the highlighter would otherwise color numbers, UUIDs, and paths,
    breaking assertions in tests and muddling run.log transcripts.
    """

    def __init__(self, *args, **kwargs) -> None:
        """Initialize with auto-highlighting disabled by default."""
        kwargs.setdefault("highlight", False)
        super().__init__(*args, **kwargs)

    def print(self, *args, **kwargs) -> None:  # type: ignore[override]
        """Print to the terminal, then tee a de-styled plaintext copy to run.log."""
        if args and all(isinstance(a, str) for a in args):
            kwargs.setdefault("soft_wrap", True)
        super().print(*args, **kwargs)
        with self.capture() as captured:
            super().print(*args, **kwargs)
        _tee(Text.from_ansi(captured.get()).plain.rstrip("\n"))


console = TeeConsole()
