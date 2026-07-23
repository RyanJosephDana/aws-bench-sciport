from aws_bench.exceptions import AWSBenchError


class CustomScriptError(AWSBenchError):
    """Base exception for custom script errors."""


class ScriptExecutionError(CustomScriptError):
    """Raised when a Script execution didn't succeed."""


class ScriptUploadError(CustomScriptError):
    """Raised when uploading the script directory to the container fails."""


class ScriptOutputDownloadError(CustomScriptError):
    """Raised when downloading script output from the container fails."""


class ScriptResultFileNotFoundError(CustomScriptError, FileNotFoundError):
    """Raised when the expected result file is missing after the script ran.

    Pre-invoke scripts must write their result file even when there are no
    placeholders to declare (an empty JSON object ``{}`` is valid). A missing
    file usually means the script crashed before finishing.
    """


class ScriptResultFileEmptyError(CustomScriptError):
    """Raised when the result file exists but is zero bytes."""


class ScriptResultParseError(CustomScriptError):
    """Raised when the result file is not valid JSON."""
