"""aws-bench — evaluate AI agents on real-world AWS tasks."""

from __future__ import annotations

import importlib.metadata
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aws_bench.account_management.exceptions import (
        AccountCreationError,
        AccountCreationTimeoutError,
        AccountManagementError,
        NotManagementAccountError,
        OrganizationNotReadyError,
        TestEnvironmentNotFoundError,
    )
    from aws_bench.account_management.manager import AccountManager
    from aws_bench.account_management.models import (
        OrgInfo,
        ScenarioAccount,
        TestEnvironment,
    )
    from aws_bench.exceptions import (
        AWSBenchError,
        CredentialError,
    )
    from aws_bench.utils.credentials_provider import CredentialProvider

__version__ = importlib.metadata.version("aws-bench")

_LAZY_IMPORTS = {
    # Account management
    "AccountManager": ("aws_bench.account_management.manager", "AccountManager"),
    "OrgInfo": ("aws_bench.account_management.models", "OrgInfo"),
    "ScenarioAccount": ("aws_bench.account_management.models", "ScenarioAccount"),
    "TestEnvironment": ("aws_bench.account_management.models", "TestEnvironment"),
    # Credentials
    "CredentialProvider": ("aws_bench.utils.credentials_provider", "CredentialProvider"),
    # Top-level exceptions
    "AWSBenchError": ("aws_bench.exceptions", "AWSBenchError"),
    "CredentialError": ("aws_bench.exceptions", "CredentialError"),
    # Account management exceptions
    "AccountManagementError": (
        "aws_bench.account_management.exceptions",
        "AccountManagementError",
    ),
    "NotManagementAccountError": (
        "aws_bench.account_management.exceptions",
        "NotManagementAccountError",
    ),
    "OrganizationNotReadyError": (
        "aws_bench.account_management.exceptions",
        "OrganizationNotReadyError",
    ),
    "TestEnvironmentNotFoundError": (
        "aws_bench.account_management.exceptions",
        "TestEnvironmentNotFoundError",
    ),
    "AccountCreationError": (
        "aws_bench.account_management.exceptions",
        "AccountCreationError",
    ),
    "AccountCreationTimeoutError": (
        "aws_bench.account_management.exceptions",
        "AccountCreationTimeoutError",
    ),
}


def __getattr__(name: str) -> object:
    if name in _LAZY_IMPORTS:
        import importlib

        module_path, attr_name = _LAZY_IMPORTS[name]
        module = importlib.import_module(module_path)
        return getattr(module, attr_name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    # Account management
    "AccountManager",
    "OrgInfo",
    "ScenarioAccount",
    "TestEnvironment",
    # Credentials
    "CredentialProvider",
    # Exceptions
    "AWSBenchError",
    "AccountManagementError",
    "NotManagementAccountError",
    "OrganizationNotReadyError",
    "TestEnvironmentNotFoundError",
    "AccountCreationError",
    "AccountCreationTimeoutError",
    "CredentialError",
]
