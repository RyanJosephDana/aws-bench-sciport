"""Utilities for computing deterministic hashes of scenario directories."""

import hashlib
from pathlib import Path

from aws_bench.logging.logger import get_logger

logger = get_logger(__name__)


def compute_scenario_hash(scenario_dir: Path) -> str:
    """Compute deterministic SHA256 hash of the whole scenario directory.

    Hashes every file under ``scenario_dir`` (scenario.toml, phase scripts, and
    the scenario/ build context), so a change to any of them invalidates the
    baseline. Files are sorted lexicographically for deterministic output.

    Args:
        scenario_dir: Path to scenario root (contains scenario.toml)

    Returns:
        Hex-encoded SHA256 hash (64 characters)

    Raises:
        ValueError: If scenario_dir is not a directory
    """
    if not scenario_dir.is_dir():
        raise ValueError(f"Scenario directory not found: {scenario_dir}")

    hasher = hashlib.sha256()

    for file_path in sorted(scenario_dir.rglob("*")):
        # Skip symlinks entirely (security + avoid infinite loops)
        if file_path.is_symlink():
            continue
        if not file_path.is_file():
            continue

        if file_path.name in {".DS_Store", "Thumbs.db"}:
            continue

        rel_path = file_path.relative_to(scenario_dir)
        hasher.update(str(rel_path).encode())
        hasher.update(file_path.read_bytes())

    hash_value = hasher.hexdigest()
    logger.debug(f"Computed hash {hash_value[:8]}... for {scenario_dir}")
    return hash_value
