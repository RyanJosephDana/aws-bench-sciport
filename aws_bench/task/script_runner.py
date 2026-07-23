"""Run pre_invoke/post_invoke scripts inside the trial environment container.

Upload-execute-download pattern:
1. Upload the script directory into the container
2. Run the entry script via the environment's command channel
3. Download stdout + result files from the container

The entry script is ``<type>.sh`` (Linux/macOS) or ``<type>.bat`` (Windows);
helper files may live alongside it and are uploaded with the directory.

Task directory structure:
    task_dir/
    ├── pre_invoke/      → uploaded to /pre_invoke/
    │   └── pre_invoke.sh   (or pre_invoke.bat on Windows; helpers may sit alongside)
    ├── post_invoke/     → uploaded to /post_invoke/
    │   └── post_invoke.sh  (or post_invoke.bat on Windows)
    ├── tests/           → uploaded to /tests/ (by Verifier)
    └── ...

Output is written to /logs/{script_type}/ inside the container, the same
convention the verifier uses for /logs/verifier/ test output.
"""

import json
import logging
from pathlib import Path, PurePosixPath

from harbor.environments.base import BaseEnvironment
from harbor.models.trial.paths import EnvironmentPaths, TrialPaths
from harbor.utils.scripts import (
    build_execution_command,
    discover_script,
    needs_chmod,
    quote_shell_arg,
)

from aws_bench.dataset.models import ScriptType
from aws_bench.logging.logger import get_logger
from aws_bench.task.exceptions import (
    ScriptExecutionError,
    ScriptOutputDownloadError,
    ScriptResultFileEmptyError,
    ScriptResultFileNotFoundError,
    ScriptResultParseError,
    ScriptUploadError,
)

logger = get_logger(__name__)

DEFAULT_TIMEOUT = 600


class ScriptRunner:
    """Run pre_invoke or post_invoke scripts inside the trial container.

    Same upload-run-download shape the verifier uses: uploads
    task_dir/{type}/ → /{type}/, runs {type}.sh, downloads /logs/{type}/.
    """

    def __init__(
        self,
        script_type: ScriptType,
        task_dir: Path,
        trial_paths: TrialPaths,
        environment: BaseEnvironment,
        override_env: dict[str, str] | None = None,
        timeout_sec: float | None = None,
        script_logger: logging.Logger | None = None,
    ):
        """Initialize ScriptRunner.

        Args:
            script_type: PRE_INVOKE or POST_INVOKE.
            task_dir: Path to the task directory containing the script subdirectory.
            trial_paths: TrialPaths for host-side output location.
            environment: The container environment to execute in.
            override_env: Env vars passed to the script (resolved placeholders + creds).
            timeout_sec: Max execution time in seconds.
            script_logger: Optional logger override.
        """
        self._script_type = script_type
        self._task_dir = task_dir
        self._trial_paths = trial_paths
        self._environment = environment
        self._override_env: dict[str, str] = dict(override_env) if override_env else {}
        self._timeout_sec = int(timeout_sec or DEFAULT_TIMEOUT)
        self._logger = (script_logger or logger).getChild(f"{__name__}.{script_type.value}")

    @property
    def _host_script_dir(self) -> Path:
        """Source on host: task_dir/{pre_invoke|post_invoke}/."""
        return self._task_dir / self._script_type.value

    @property
    def _env_paths(self) -> EnvironmentPaths:
        """In-container path layout (e.g. /logs vs C:/logs) for the active env's OS."""
        return EnvironmentPaths.for_os(self._environment.os)

    @property
    def _container_script_dir(self) -> PurePosixPath:
        """Where scripts upload to: /{pre_invoke|post_invoke}/ (or the C:/ variant on Windows)."""
        return self._env_paths.logs_dir.parent / self._script_type.value

    @property
    def _container_output_dir(self) -> PurePosixPath:
        """Where output goes in container: /logs/{pre_invoke|post_invoke}/ (or C:/ on Windows)."""
        return self._env_paths.logs_dir / self._script_type.value

    @property
    def _host_output_dir(self) -> Path:
        """Where output lands on host: trial_dir/{pre_invoke|post_invoke}/."""
        return self._trial_paths.trial_dir / self._script_type.value

    @property
    def _entry_script(self) -> str:
        """Filename of the entry script for this phase.

        Looks up ``<phase>.sh`` (Linux/macOS) or ``<phase>.bat`` (Windows)
        inside the host script directory and returns just the filename
        (e.g. ``pre_invoke.sh``). Callers join it with the relevant host
        or container directory.

        Raises:
            FileNotFoundError: When neither variant exists in the host
                script directory for this phase.
        """
        discovered = discover_script(
            self._host_script_dir,
            self._script_type.value,
            task_os=self._environment.os,
        )
        if discovered is None:
            phase = self._script_type.value
            raise FileNotFoundError(
                f"No {self._environment.os.value} {phase} entry script found: "
                f"expected {self._host_script_dir / f'{phase}.sh'} "
                f"or {self._host_script_dir / f'{phase}.bat'}"
            )
        return discovered.name

    def _parse_result_json(self, path: Path) -> dict[str, str]:
        """Parse a result JSON file. Must be a flat {string: string} object."""
        if path.stat().st_size == 0:
            raise ScriptResultFileEmptyError(f"Result file is empty: {path}")
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError as e:
            raise ScriptResultParseError(f"Failed to parse result file {path} as JSON") from e

    async def run(self, output_file_name: str | None = None) -> dict[str, str]:
        """Run the script inside the container.

        The script runs with cwd set to /{type}/. If output_file_name is
        provided, the script MUST write a JSON file at
        /logs/{type}/{output_file_name} — paths inside the container are
        fixed by convention, the same way the verifier expects its reward
        file at a known path. Use ``{}`` to explicitly signal "no values
        to return".

        Args:
            output_file_name: Required JSON output filename in /logs/{type}/
                (e.g. "placeholder.json"). None if no output is expected.

        Returns:
            Parsed output dict (empty when output_file_name is None).

        Raises:
            ScriptUploadError: If script directory cannot be uploaded.
            ScriptOutputDownloadError: If output dir cannot be downloaded.
            ScriptExecutionError: If script exits with non-zero code.
            ScriptResultFileNotFoundError: If output_file_name was requested
                but the script did not produce the file.
            ScriptResultFileEmptyError: If the result file is zero bytes.
            ScriptResultParseError: If the result file is not valid JSON.
        """
        if not self._host_script_dir.exists():
            raise FileNotFoundError(f"Script directory not found: {self._host_script_dir}")

        self._host_output_dir.mkdir(parents=True, exist_ok=True)

        # 1. Upload script directory into container
        try:
            await self._environment.upload_dir(
                source_dir=self._host_script_dir,
                target_dir=str(self._container_script_dir),
            )
        except Exception as e:
            raise ScriptUploadError(f"Failed to upload {self._host_script_dir} to container") from e

        # 2. Prepare container: create output dir + set permissions
        container_entry = self._container_script_dir / self._entry_script

        await self._environment.exec(
            command=f"mkdir -p {self._container_output_dir}",
            user="root",
        )

        if needs_chmod(str(container_entry)):
            await self._environment.exec(
                command=(f"chmod +x {quote_shell_arg(str(container_entry), None)}"),
                user="root",
            )

        # 3. Run script (stdout/stderr → /logs/{type}/stdout.log)
        stdout_path = self._container_output_dir / "stdout.log"
        command = build_execution_command(
            str(container_entry),
            stdout_path=str(stdout_path),
        )

        self._logger.debug(
            f"Running in container: {container_entry} (timeout={self._timeout_sec}s)"
        )

        exec_result = await self._environment.exec(
            command=command,
            cwd=str(self._container_script_dir),
            env=self._override_env if self._override_env else None,
            timeout_sec=self._timeout_sec,
        )

        # 4. Download output logs unconditionally — /logs/{type}/ is not
        #    one of the directories the environment auto-syncs to the host
        #    when capabilities.mounted is True, so we cannot skip the
        #    download for mount-capable backends.
        try:
            await self._environment.download_dir(
                source_dir=str(self._container_output_dir),
                target_dir=self._host_output_dir,
            )
        except Exception as e:
            raise ScriptOutputDownloadError("Failed to download output from container") from e

        # 5. Check exit code
        if exec_result.return_code != 0:
            self._logger.error(
                f"Script exited with code {exec_result.return_code}. "
                f"Logs at {self._host_output_dir / 'stdout.log'}"
            )
            raise ScriptExecutionError(
                f"{self._script_type.value} script failed with exit code {exec_result.return_code}"
            )

        # 6. Parse result file from already-downloaded /logs/{type}/ dir.
        #    The script must always write the result file when output_file_name
        #    is requested — empty `{}` is valid for "no placeholders to declare",
        #    but a missing file means the script crashed before finishing.
        result: dict[str, str] = {}
        if output_file_name:
            host_result_path = self._host_output_dir / output_file_name
            if not host_result_path.exists():
                raise ScriptResultFileNotFoundError(
                    f"Expected result file not found at {host_result_path}. "
                    f"{self._script_type.value} script must write {output_file_name} "
                    f"to /logs/{self._script_type.value}/ (use '{{}}' if no values)."
                )
            result = self._parse_result_json(host_result_path)

        # 7. Clean up container (remove scripts + logs so agent cannot see them)
        if self._script_type == ScriptType.PRE_INVOKE:
            await self._environment.exec(
                command=(f"rm -rf {self._container_script_dir} {self._container_output_dir}"),
                user="root",
            )

        return result
