"""The discovery Lambda handler must import from the zip without the heavy deps.

The ``python3.12`` Lambda runtime provides the standard library plus ``boto3`` /
``botocore`` and their bundled dependencies — but none of the heavy packages the
rest of ``aws_bench`` pulls in (``rich``, ``pydantic``, ``harbor``, ``tenacity``…).
If the handler's import closure requires one, the function fails at cold start and
every scan silently falls back to the host path. This test builds the real
deployment zip, extracts it, and imports the handler in a subprocess where the zip
shadows the editable install and the heavy packages are made unimportable — so a
closure that regrows one of them fails here instead of in production.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import zipfile
from pathlib import Path

from aws_bench.resource_management.fastscan.lambda_deploy import _build_zip

# Heavy third-party packages absent from the Lambda runtime (it provides boto3/botocore
# and their bundled deps, none of these). The handler's closure must never require one;
# blocking exactly this set expresses the invariant without modeling boto3's dep tree.
_FORBIDDEN = ("rich", "harbor", "pydantic", "pydantic_core", "tenacity", "shortuuid")

# Runs in the subprocess: make the forbidden packages unimportable (as they are
# in-Lambda) via an import guard, then import the handler from the extracted zip. A
# statement-level import of any forbidden package in the closure routes through the
# guard and raises.
_IMPORT_PROBE = f"""
import builtins
import sys

_forbidden = {set(_FORBIDDEN)!r}
_real = builtins.__import__


def _guarded_import(name, *args, **kwargs):
    if name.split(".")[0] in _forbidden:
        raise ModuleNotFoundError(f"forbidden in the Lambda closure: {{name}}")
    return _real(name, *args, **kwargs)


builtins.__import__ = _guarded_import

import aws_bench.resource_management.fastscan.lambda_handler as handler

assert hasattr(handler, "handler"), "handler entry point missing"

# Self-guard: prove the handler resolved from the extracted zip, not the ambient
# editable install. Without this, a zip that failed to shadow the install would
# import the host copy (with its heavy deps available) and pass vacuously — the
# EXTRACT_DIR sentinel is substituted in from the test below.
_extract = {"__EXTRACT_DIR__"!r}
assert handler.__file__.startswith(_extract), (
    f"handler imported from {{handler.__file__}}, not the extracted zip {{_extract}}"
)
print("IMPORT_OK")
"""


def test_handler_imports_from_zip_without_forbidden_deps(tmp_path: Path) -> None:
    extract = tmp_path / "pkg"
    extract.mkdir()
    with zipfile.ZipFile(io.BytesIO(_build_zip())) as zf:
        zf.extractall(extract)

    # Extracted zip first on the path so its ``aws_bench`` shadows the editable
    # install — the handler must import from what the zip actually ships. PYTHONSAFEPATH
    # keeps the interpreter from prepending an entry (e.g. the cwd) that could re-shadow it.
    pythonpath = os.pathsep.join([str(extract), *sys.path])
    probe = _IMPORT_PROBE.replace("__EXTRACT_DIR__", str(extract))
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        env={**os.environ, "PYTHONPATH": pythonpath, "PYTHONSAFEPATH": "1"},
        capture_output=True,
        text=True,
    )

    assert "IMPORT_OK" in proc.stdout, (
        "handler failed to import from the zip without the forbidden heavy deps:\n"
        f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )
