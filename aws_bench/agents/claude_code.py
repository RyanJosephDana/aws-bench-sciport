"""Plugin-capable Claude Code agent for aws-bench.

Extends Harbor's ``claude-code`` agent so a headless ``claude --print`` run can
install Claude Code plugins into its per-trial config directory before the task
runs. A plugin bundles MCP servers and skills together, so installing one makes
both available to the agent.

Marketplaces and plugins are supplied as constructor kwargs, so they flow from
``--ak`` on the run CLI::

    -a claude-code \
      --ak marketplaces='["anthropics/claude-plugins-official"]' \
      --ak plugins='["aws-core@claude-plugins-official"]'

With no ``plugins``, no plugin install runs.

It also stands the benchmark arm's tooling up before the task, when the run
mounts one. See :func:`_arm_setup_command`.
"""

from __future__ import annotations

import shlex
from pathlib import Path

from harbor.agents.installed.claude_code import ClaudeCode as _HarborClaudeCode
from harbor.environments.base import BaseEnvironment

# claude installs to ~/.local/bin, which is off the PATH for the non-interactive
# exec shell; prepend it so `claude plugin install` resolves.
_PATH_RESTORE = 'export PATH="$HOME/.local/bin:$PATH"'

# Where a benchmark run bind-mounts the pieces prepared by
# benchmarks/agent-env/prepare.py --export. Set as environment config on the job.
_TOOLCHAIN_MOUNT = "AWS_BENCH_TOOLCHAIN"
_ARM_SOURCE_MOUNT = "AWS_BENCH_ARM_SRC"
_ARM_WORKDIR = "AWS_BENCH_ARM_WORKDIR"

# Runs as root before the task. Names are substituted, values are read in the
# container. A missing mount exits nonzero: a half-built arm is how a tool ends
# up silently unavailable and the trial scores from jq instead.
_ARM_SETUP_SCRIPT = """
set -e
[ -n "${{{workdir}:-}}" ] && [ -n "${{{source}:-}}" ] || exit 0

if [ ! -d "${{{source}}}" ]; then
    echo "aws-bench: {source} points at ${{{source}}}, which is not mounted" >&2
    exit 1
fi

if [ -n "${{{toolchain}:-}}" ]; then
    if [ ! -d "${{{toolchain}}}" ]; then
        echo "aws-bench: {toolchain} points at ${{{toolchain}}}, which is not mounted" >&2
        exit 1
    fi
    # Symlink rather than export PATH: each agent command gets a fresh shell
    # that inherits nothing exported here.
    for dir in "${{{toolchain}}}"/*/bin "${{{toolchain}}}"/pulumi; do
        [ -d "$dir" ] && ln -sf "$dir"/* /usr/local/bin/ 2>/dev/null || true
    done
fi

# git, because a tool may keep its state in one. chant records a lifecycle
# snapshot on an orphan branch and reads it back with `git ls-tree`; the
# dataset's task image has no git, so that read returned nothing and
# `chant search --at latest` reported "No snapshots found" — a tool answering
# from recorded state looking exactly like a tool with no recorded state.
if ! command -v git >/dev/null 2>&1; then
    if command -v apt-get >/dev/null 2>&1; then
        apt-get update >/dev/null 2>&1 && apt-get install -y git >/dev/null 2>&1
    elif command -v apk >/dev/null 2>&1; then
        apk add --no-cache git >/dev/null 2>&1
    elif command -v yum >/dev/null 2>&1; then
        yum install -y git >/dev/null 2>&1
    fi
fi
command -v git >/dev/null 2>&1 || {{
    echo "aws-bench: git is unavailable and could not be installed; a tool that keeps state in git cannot read it" >&2
    exit 1
}}

rm -rf "${{{workdir}}}"
mkdir -p "$(dirname "${{{workdir}}}")"
cp -a "${{{source}}}" "${{{workdir}}}"
# The copy is made as root and the agent runs as someone else. Without both of
# these the workspace is readable and useless: git refuses a repo it considers
# dubiously owned, and a tool that writes (terraform's .terraform, cdk's
# cdk.out) fails on a directory it cannot write.
git config --system --add safe.directory '*' >/dev/null 2>&1 || true
chmod -R a+rwX "${{{workdir}}}" 2>/dev/null || true
# An arm's own launchers go on PATH. Without this the agent types the tool's
# name, gets "command not found", and spends two or three turns finding the
# binary before any work starts — 27 failed invocations and 22 hunting turns
# across one 24-trial run. Every other arm's tool is already on PATH, so this
# was a tax on exactly one of them.
if [ -d "${{{workdir}}}/bin" ]; then
    for exe in "${{{workdir}}}"/bin/*; do
        [ -x "$exe" ] || continue
        # A wrapper, not a symlink. These launchers locate their own package
        # with `dirname $0`, so a symlink on PATH resolves the root to
        # /usr/local and the tool dies with "exec: …/node_modules/…: not found"
        # — worse than absent, because the agent gets a confusing error instead
        # of a clean "command not found".
        name=$(basename "$exe")
        printf '#!/bin/sh\nexec "%s" "$@"\n' "$exe" > "/usr/local/bin/$name" 2>/dev/null || continue
        chmod +x "/usr/local/bin/$name" 2>/dev/null || true
    done
fi
"""


class ClaudeCode(_HarborClaudeCode):
    """Claude Code that installs plugins from marketplaces per trial."""

    def __init__(
        self,
        logs_dir: Path,
        marketplaces: list[str] | None = None,
        plugins: list[str] | None = None,
        arm_src: str | None = None,
        arm_workdir: str | None = None,
        toolchain: str | None = None,
        *args,
        **kwargs,
    ):
        """Store the marketplaces, plugins, and benchmark arm layout.

        Args:
            logs_dir: Trial logs directory, forwarded to the base agent.
            marketplaces: Marketplace git sources to add, each an ``owner/repo``
                slug or a clone URL.
            plugins: Plugin specs to install, each ``<plugin>@<marketplace>``.
            arm_src: Container path where the arm's prepared workspace is mounted
                read-only. Given together with ``arm_workdir``.
            arm_workdir: Writable container path the workspace is copied to,
                matching the path the arm's briefing sends the agent to.
            toolchain: Container path where the exported toolchain is mounted;
                its binaries are symlinked onto ``PATH``.
            *args: Forwarded to the base agent.
            **kwargs: Forwarded to the base agent.
        """
        self._marketplaces = list(marketplaces) if marketplaces else []
        self._plugins = list(plugins) if plugins else []
        if self._marketplaces and not self._plugins:
            raise ValueError(
                "marketplaces were given without plugins; a marketplace is only "
                "added when a plugin from it is installed"
            )
        if bool(arm_src) != bool(arm_workdir):
            raise ValueError(
                "arm_src and arm_workdir go together; one without the other would "
                "leave the arm's tooling unavailable to the agent"
            )
        self._arm_src = arm_src
        self._arm_workdir = arm_workdir
        self._toolchain = toolchain
        super().__init__(logs_dir, *args, **kwargs)

    async def install(self, environment: BaseEnvironment) -> None:
        """Install Claude Code, the benchmark arm's tooling, and git for plugins.

        Claude Code shells out to ``git`` to clone a plugin marketplace, but the
        agent base image does not ship it. Install it during agent setup using
        whichever package manager the image provides. The container has no GitHub
        SSH key, so GitHub SSH remotes are rewritten to anonymous HTTPS for the
        public marketplace clone.
        """
        await super().install(environment)
        await self._setup_arm(environment)

        if not self._plugins:
            return

        await self.exec_as_root(
            environment,
            command=(
                "if ! command -v git >/dev/null 2>&1; then "
                "if command -v apt-get >/dev/null 2>&1; then "
                "apt-get update >/dev/null 2>&1 && apt-get install -y git >/dev/null 2>&1; "
                "elif command -v apk >/dev/null 2>&1; then "
                "apk add --no-cache git >/dev/null 2>&1; "
                "elif command -v yum >/dev/null 2>&1; then "
                "yum install -y git >/dev/null 2>&1; "
                "fi; fi"
            ),
            env={"DEBIAN_FRONTEND": "noninteractive"},
        )
        await self.exec_as_agent(
            environment,
            command=('git config --global url."https://github.com/".insteadOf "git@github.com:"'),
        )

    async def _setup_arm(self, environment: BaseEnvironment) -> None:
        """Put the benchmark arm's tooling and workspace in place, or fail loudly.

        Nothing happens unless the run mounts an arm, so ordinary tasks are
        unaffected. When it does, two things have to be true before the agent
        starts, and in the scenario-1 runs neither was:

        The tool has to exist. The dataset's task image is python:3.13-slim plus
        the AWS CLI and jq, with no JavaScript or IaC runtime, so the CDK and
        Alchemy arms could not run a single ``cdk`` or ``alchemy`` command. The
        agent quietly answered from ``jq`` over raw state instead and still
        scored, which makes the result a measurement of jq, not of the tool.

        The workspace has to be writable. The arm was bind-mounted read-only, so
        ``terraform init`` could not write ``.terraform/`` — leaving
        ``show -json`` refusing — and ``cdk ls`` died on
        ``EROFS ... cdk.out/synth.lock``. Copying to a writable path also keeps a
        trial from mutating the shared arm directory.

        A failure here raises rather than degrading, because a degraded arm
        produces rewards that look exactly like real ones.
        """
        if not self._arm_workdir or not self._arm_src:
            return

        # Supplied explicitly to the exec rather than read from the container's
        # own environment: --agent-env lands on the agent process, and this runs
        # as root before the agent exists.
        env = {_ARM_WORKDIR: self._arm_workdir, _ARM_SOURCE_MOUNT: self._arm_src}
        if self._toolchain:
            env[_TOOLCHAIN_MOUNT] = self._toolchain

        command = _ARM_SETUP_SCRIPT.format(
            workdir=_ARM_WORKDIR,
            source=_ARM_SOURCE_MOUNT,
            toolchain=_TOOLCHAIN_MOUNT,
        )
        await self.exec_as_root(environment, command=command, env=env)

    def _build_register_mcp_servers_command(self) -> str | None:
        """Chain plugin installation onto the per-trial config setup command.

        Harbor sets ``CLAUDE_CONFIG_DIR`` for this command, so the marketplace,
        plugin cache, and ``enabledPlugins`` entry land in the per-trial config
        directory. A failed ``plugin install`` aborts setup; the task does not
        run without its plugin.
        """
        base_cmd = super()._build_register_mcp_servers_command()

        if not self._plugins:
            return base_cmd

        parts = [_PATH_RESTORE]
        for source in self._marketplaces:
            parts.append(f"claude plugin marketplace add {shlex.quote(source)} 2>/dev/null || true")
        for spec in self._plugins:
            parts.append(f"claude plugin install {shlex.quote(spec)} --scope user")
        plugin_cmd = "; ".join(parts)

        if base_cmd:
            return f"{base_cmd} && {plugin_cmd}"
        return plugin_cmd
