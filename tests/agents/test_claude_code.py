"""Tests for the plugin-capable Claude Code agent."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from harbor.agents.factory import AgentFactory
from harbor.models.agent.name import AgentName

from aws_bench.agents.claude_code import ClaudeCode

_MARKETPLACE = "anthropics/claude-plugins-official"
_PLUGIN = "aws-core@claude-plugins-official"


@pytest.fixture
def logs_dir(tmp_path: Path) -> Path:
    return tmp_path / "logs"


@pytest.fixture
def plugin_agent(logs_dir: Path) -> ClaudeCode:
    return ClaudeCode(logs_dir=logs_dir, marketplaces=[_MARKETPLACE], plugins=[_PLUGIN])


def _fresh_environment() -> MagicMock:
    environment = MagicMock()
    environment.exec = AsyncMock(return_value=MagicMock(return_code=0, stdout="", stderr=""))
    return environment


def _commands(environment: MagicMock) -> list[str]:
    return [c.kwargs.get("command", "") for c in environment.exec.call_args_list]


# ── name ──


def test_name_is_claude_code():
    """Keeps the built-in name so it overrides Harbor's claude-code."""
    assert ClaudeCode.name() == "claude-code"


# ── factory override ──


def test_factory_resolves_claude_code_to_subclass():
    """Importing aws_bench.agents maps `claude-code` to this subclass."""
    import aws_bench.agents  # noqa: F401

    assert AgentFactory._AGENT_MAP[AgentName("claude-code")] is ClaudeCode


def test_subclass_replaces_builtin_in_agents_list():
    """The subclass replaces Harbor's claude-code in the factory list."""
    import aws_bench.agents  # noqa: F401

    claude_entries = [a for a in AgentFactory._AGENTS if a.name() == "claude-code"]
    assert claude_entries == [ClaudeCode]


# ── init ──


def test_defaults_to_no_plugins(logs_dir: Path):
    """Defaults to no marketplaces or plugins."""
    agent = ClaudeCode(logs_dir=logs_dir)
    assert agent._marketplaces == []
    assert agent._plugins == []


def test_stores_marketplaces_and_plugins(plugin_agent: ClaudeCode):
    """Stores the supplied marketplaces and plugins."""
    assert plugin_agent._marketplaces == [_MARKETPLACE]
    assert plugin_agent._plugins == [_PLUGIN]


def test_none_normalizes_to_empty_list(logs_dir: Path):
    """Normalizes explicit None to empty lists."""
    agent = ClaudeCode(logs_dir=logs_dir, marketplaces=None, plugins=None)
    assert agent._marketplaces == []
    assert agent._plugins == []


def test_marketplaces_without_plugins_raises(logs_dir: Path):
    """A marketplace with no plugin to install is a no-op, so reject it loudly."""
    with pytest.raises(ValueError, match="marketplaces were given without plugins"):
        ClaudeCode(logs_dir=logs_dir, marketplaces=[_MARKETPLACE], plugins=None)


# ── MCP / plugin setup command ──


def test_no_plugins_passes_through_to_base(logs_dir: Path):
    """Returns the base command (None here) when no plugins are requested."""
    agent = ClaudeCode(logs_dir=logs_dir)
    assert agent._build_register_mcp_servers_command() is None


def test_installs_each_plugin(plugin_agent: ClaudeCode):
    """Installs each requested plugin user-scoped."""
    cmd = plugin_agent._build_register_mcp_servers_command()
    assert cmd is not None
    assert f"claude plugin install {_PLUGIN} --scope user" in cmd


def test_adds_each_marketplace(plugin_agent: ClaudeCode):
    """Adds each requested marketplace."""
    cmd = plugin_agent._build_register_mcp_servers_command()
    assert cmd is not None
    assert f"claude plugin marketplace add {_MARKETPLACE}" in cmd


def test_restores_path(plugin_agent: ClaudeCode):
    """Restores PATH so claude resolves under the minimal exec PATH."""
    cmd = plugin_agent._build_register_mcp_servers_command()
    assert cmd is not None
    assert "export PATH=" in cmd


def test_adds_marketplace_before_installing_plugin(plugin_agent: ClaudeCode):
    """Adds the marketplace before installing the plugin that needs it."""
    cmd = plugin_agent._build_register_mcp_servers_command()
    assert cmd is not None
    assert cmd.index("marketplace add") < cmd.index("plugin install")


def test_supports_multiple_plugins(logs_dir: Path):
    """Adds every marketplace and installs every plugin."""
    agent = ClaudeCode(
        logs_dir=logs_dir,
        marketplaces=["a/one", "b/two"],
        plugins=["p1@one", "p2@two"],
    )
    cmd = agent._build_register_mcp_servers_command()
    assert cmd is not None
    assert "claude plugin install p1@one --scope user" in cmd
    assert "claude plugin install p2@two --scope user" in cmd
    assert "marketplace add a/one" in cmd
    assert "marketplace add b/two" in cmd


def test_chains_after_base_mcp_servers(logs_dir: Path):
    """Writes task MCP servers first, then chains plugin install with &&."""
    stdio = MagicMock(transport="stdio", command="run", args=[])
    stdio.name = "srv"
    agent = ClaudeCode(logs_dir=logs_dir, plugins=[_PLUGIN], mcp_servers=[stdio])
    cmd = agent._build_register_mcp_servers_command()
    assert cmd is not None
    assert ".claude.json" in cmd
    assert cmd.index(".claude.json") < cmd.index("plugin install")
    assert " && " in cmd


# ── install ──


@pytest.mark.asyncio
async def test_no_plugins_skips_git_install(logs_dir: Path):
    """Skips git install when no plugins are requested."""
    agent = ClaudeCode(logs_dir=logs_dir)
    environment = _fresh_environment()

    await agent.install(environment)

    assert not any("command -v git" in c for c in _commands(environment))


@pytest.mark.asyncio
async def test_installs_git_when_plugins_requested(plugin_agent: ClaudeCode):
    """Installs git during setup when plugins are requested."""
    environment = _fresh_environment()

    await plugin_agent.install(environment)

    assert any("command -v git" in c for c in _commands(environment))


@pytest.mark.asyncio
async def test_rewrites_github_ssh_to_https(plugin_agent: ClaudeCode):
    """Rewrites GitHub SSH remotes to anonymous HTTPS for the clone."""
    environment = _fresh_environment()

    await plugin_agent.install(environment)

    assert any("insteadOf" in c and "git@github.com:" in c for c in _commands(environment))


# ── benchmark arm setup ──

_ARM_SRC = "/opt/awsbench-arm"
_ARM_WORKDIR = "/workspace/chant"
_TOOLCHAIN = "/opt/awsbench-toolchain"


@pytest.fixture
def arm_agent(logs_dir: Path) -> ClaudeCode:
    return ClaudeCode(
        logs_dir=logs_dir,
        arm_src=_ARM_SRC,
        arm_workdir=_ARM_WORKDIR,
        toolchain=_TOOLCHAIN,
    )


def _envs(environment: MagicMock) -> list[dict[str, str]]:
    return [c.kwargs.get("env") or {} for c in environment.exec.call_args_list]


def test_arm_defaults_to_unset(logs_dir: Path):
    """No arm mounted means ordinary tasks are untouched."""
    agent = ClaudeCode(logs_dir=logs_dir)

    assert agent._arm_src is None
    assert agent._arm_workdir is None


def test_arm_src_without_workdir_raises(logs_dir: Path):
    """Half a mount would leave the arm's tooling unavailable to the agent."""
    with pytest.raises(ValueError, match="go together"):
        ClaudeCode(logs_dir=logs_dir, arm_src=_ARM_SRC)


def test_arm_workdir_without_src_raises(logs_dir: Path):
    """The reverse half is just as broken."""
    with pytest.raises(ValueError, match="go together"):
        ClaudeCode(logs_dir=logs_dir, arm_workdir=_ARM_WORKDIR)


@pytest.mark.asyncio
async def test_no_arm_setup_without_an_arm(logs_dir: Path):
    """A run with no arm mounted issues no arm setup."""
    agent = ClaudeCode(logs_dir=logs_dir)
    environment = _fresh_environment()

    await agent.install(environment)

    assert not any(_ARM_SRC in c for c in _commands(environment))


@pytest.mark.asyncio
async def test_copies_arm_to_a_writable_workdir(arm_agent: ClaudeCode):
    """The read-only mount is copied out; terraform and cdk need somewhere to write."""
    environment = _fresh_environment()

    await arm_agent.install(environment)

    setup = next(c for c in _commands(environment) if "AWS_BENCH_ARM_SRC" in c)
    assert '_share "${AWS_BENCH_ARM_SRC}" "${AWS_BENCH_ARM_WORKDIR}"' in setup


@pytest.mark.asyncio
async def test_symlinks_the_toolchain_onto_path(arm_agent: ClaudeCode):
    """Each agent command gets a fresh shell, so PATH is not exported but linked."""
    environment = _fresh_environment()

    await arm_agent.install(environment)

    setup = next(c for c in _commands(environment) if "AWS_BENCH_TOOLCHAIN" in c)
    assert "ln -sf" in setup and "/usr/local/bin/" in setup
    # terraform-bin holds its binary at the root, not under bin/, so the glob
    # alone misses it — the terraform-i1 audit refusal ("terraform was not on
    # PATH in 24 trials") is what this line looks like when it is absent.
    assert '/terraform-bin' in setup


@pytest.mark.asyncio
async def test_passes_paths_as_exec_env(arm_agent: ClaudeCode):
    """Supplied to the root exec, not --agent-env: setup precedes the agent process."""
    environment = _fresh_environment()

    await arm_agent.install(environment)

    env = next(e for e in _envs(environment) if "AWS_BENCH_ARM_SRC" in e)
    assert env["AWS_BENCH_ARM_SRC"] == _ARM_SRC
    assert env["AWS_BENCH_ARM_WORKDIR"] == _ARM_WORKDIR
    assert env["AWS_BENCH_TOOLCHAIN"] == _TOOLCHAIN


@pytest.mark.asyncio
async def test_missing_mount_is_fatal(arm_agent: ClaudeCode):
    """A half-built arm scores from jq and looks identical, so it must not proceed."""
    environment = _fresh_environment()

    await arm_agent.install(environment)

    setup = next(c for c in _commands(environment) if "AWS_BENCH_ARM_SRC" in c)
    assert "is not mounted" in setup and "exit 1" in setup


def test_arm_setup_script_renders_and_is_valid_shell():
    """The setup template must format, and the result must be a runnable script.

    It is a `.format()` template full of shell, so every literal brace has to be
    doubled. A single one turns into a placeholder and `.format()` raises at
    trial start — which surfaces as `RuntimeError: Agent install failed`, one per
    trial, and reads as the arm scoring zero rather than as its agents never
    having started. A whole 24-trial run was lost to one undoubled `{`.
    """
    import re
    import subprocess
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "aws_bench" / "agents" / "claude_code.py"
    template = re.search(
        r'_ARM_SETUP_SCRIPT\s*=\s*(?:r?"""|r?\'\'\')(.*?)(?:"""|\'\'\')',
        src.read_text(),
        re.S,
    )
    assert template, "could not find _ARM_SETUP_SCRIPT"
    body = template.group(1)

    names = sorted(set(re.findall(r"\{(\w+)\}", body)))
    rendered = body.format(**{n: f"/fake/{n}" for n in names})

    # `sh -n` parses without executing: catches an unbalanced brace or quote that
    # only a real trial would otherwise find.
    check = subprocess.run(["sh", "-n"], input=rendered, text=True, capture_output=True)
    assert check.returncode == 0, f"rendered setup script is not valid shell:\n{check.stderr}"
