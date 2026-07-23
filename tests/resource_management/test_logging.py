"""Tests for file logging integration."""

from __future__ import annotations

import logging
from pathlib import Path

from aws_bench.logging.logger import file_logging, get_logger


def test_file_logging_captures_debug_by_default(tmp_path: Path):
    """File handler defaults to DEBUG level."""
    log_file = tmp_path / "test.log"
    logger = get_logger("aws_bench.ftest")
    with file_logging(log_file):
        logger.debug("debug-msg")
        logger.info("info-msg")
    content = log_file.read_text()
    assert "debug-msg" in content
    assert "info-msg" in content


def test_file_logging_custom_level(tmp_path: Path):
    """File handler respects a custom level."""
    log_file = tmp_path / "test.log"
    logger = get_logger("aws_bench.ftest2")
    with file_logging(log_file, level=logging.WARNING):
        logger.info("should-not-appear")
        logger.warning("should-appear")
    content = log_file.read_text()
    assert "should-not-appear" not in content
    assert "should-appear" in content
