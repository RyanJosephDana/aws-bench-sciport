"""Compute detailed benchmark metrics from a completed aws-bench run.

This is a post-run utility: run it after ``aws-bench run`` has finished. It reads
Harbor trial ``result.json`` files plus each trial's ATIF
``agent/trajectory.json`` and writes ``metrics.json`` next to the run.

Usage:
    python scripts/compute_metrics.py /path/to/jobs/<run>
    python scripts/compute_metrics.py --compute-all /path/to/jobs
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from aws_bench.metrics.aggregation import aggregate_detailed
from aws_bench.metrics.run_data import RunData, find_run_dirs, get_trial_dirs

logger = logging.getLogger(__name__)

METRICS_FILE_NAME = "metrics.json"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def compute_single_run(
    run_dir: Path,
    *,
    success_threshold: float,
    output: Path | None = None,
) -> bool:
    """Compute and write detailed metrics for a single run directory."""
    run = RunData.load(run_dir)
    if not run:
        logger.error("No valid result.json found in %s", run_dir)
        return False
    if not run.trials:
        logger.error("No valid Harbor trial directories found in %s", run.run_dir)
        return False

    metrics = aggregate_detailed(run.trials, success_threshold=success_threshold)
    metrics_path = output or (run.run_dir / METRICS_FILE_NAME)
    _write_json(metrics_path, metrics)

    logger.info("Wrote metrics to %s", metrics_path)
    return True


def _completed_run_dirs(jobs_dir: Path) -> list[Path]:
    return [run_dir for run_dir in find_run_dirs(jobs_dir) if get_trial_dirs(run_dir)]


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        description="Compute detailed metrics from completed Harbor ATIF run outputs.",
    )
    parser.add_argument(
        "path",
        type=Path,
        help="Run directory, or jobs directory when --compute-all is set.",
    )
    parser.add_argument(
        "--compute-all",
        action="store_true",
        help="Compute metrics for every completed run directory under PATH.",
    )
    parser.add_argument(
        "--success-threshold",
        type=float,
        default=1.0,
        help="Verifier reward threshold treated as a successful/correct test case.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output path for flat metrics. Only valid for a single run.",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging.")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
    )

    if args.compute_all and args.output:
        logger.error("--output is only valid for a single run")
        return 2

    if args.compute_all:
        run_dirs = _completed_run_dirs(args.path.expanduser().resolve())
        logger.info("Found %s completed run(s) under %s", len(run_dirs), args.path)
        ok = True
        for run_dir in run_dirs:
            ok = (
                compute_single_run(
                    run_dir,
                    success_threshold=args.success_threshold,
                )
                and ok
            )
        return 0 if ok else 1

    return (
        0
        if compute_single_run(
            args.path,
            success_threshold=args.success_threshold,
            output=args.output,
        )
        else 1
    )


if __name__ == "__main__":
    sys.exit(main())
