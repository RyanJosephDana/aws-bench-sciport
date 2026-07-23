"""File I/O utilities."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aws_bench.logging.logger import get_logger

logger = get_logger(__name__)


def write_json(data: Any, path: Path) -> None:
    """Write JSON data to disk, creating parent directories as needed."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        logger.debug("Saved %s", path)
    except OSError as exc:
        logger.warning("Failed to save %s: %s", path, exc)
        raise
