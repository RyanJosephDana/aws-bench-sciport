# The agent environment

An arm's score only means something if the agent could actually run that arm's
tooling. In the scenario-1 runs it often could not, and nothing said so.

## What went wrong

The dataset's task environment — the container the agent under test runs in — is
`python:3.13-slim` with the AWS CLI and `jq`. It ships no JavaScript or IaC
runtime. The scenario deploy image is a different one (`node:24-slim`, with the
CDK CLI installed), which is why the estates deployed fine and the gap went
unnoticed.

Three things followed from that, and they compound:

1. **The tool was not installed.** `npx: command not found` 22 times in the CDK
   arm, `alchemy: command not found` 30+ times in the Alchemy arm. Both arms were
   scored anyway, because an agent that cannot run `cdk ls` just runs `jq` over
   `cdk.out/*.template.json` instead and still answers.
2. **The mount was read-only.** `terraform init` could not write
   `.terraform/modules/modules.json`, so `terraform show -json` refused with
   "Required plugins are not installed". `cdk ls` died on
   `EROFS ... cdk.out/synth.lock`. Both need somewhere to write.
3. **The dependencies were built for the wrong platform.** The arms'
   `node_modules` are installed on the host, so `@esbuild/darwin-arm64` is what
   travels and chant's `npx tsx` dies under linux looking for
   `@esbuild/linux-arm64`.

Running `audit.py` over the original jobs gives the damage:

| Arm | Trials that used their own tooling |
|---|---|
| chant | 24 / 24 |
| pulumi | 24 / 24 |
| terraform | 21 / 24 |
| cdk | 2 / 24 |
| alchemy | 0 / 24 |

## The fix

`Dockerfile` makes node, bun, terraform and pulumi ambient in the agent
container, pinned to the versions the arms deployed with. Ambient rather than
vendored per arm on purpose: chant's arm carried its own `.runtime/bin` Node and
a launcher shim, which is what let it work while CDK and Alchemy silently could
not — an asymmetry that is hard to defend even though it was not deliberate.

There is no global `aws-cdk`. The CDK arm declares its own CLI in
`devDependencies`, and a global one shadows it on the bare `cdk` name: a global
2.1112.0 against the arm's `aws-cdk-lib` 2.262.0 fails with a cloud-assembly
schema mismatch printed under a wall of synth warnings. Each arm uses the CDK it
ships, via `npx cdk`.

`arms.py` holds the per-arm contract — where the workspace goes, what env it
needs, and which of its own briefing's commands must work.

## Using it

```sh
# Once per toolchain or arm change. Bakes dependencies into a layer per arm, so
# nothing installs per trial. Roughly a minute for all six, cached after that.
python3 benchmarks/agent-env/prepare.py

# Before every run. Fails if an arm cannot answer with its own tooling.
python3 benchmarks/agent-env/preflight.py

# Export for the trial containers to bind-mount. Goes to ~/.aws-bench/agent-env,
# not the repo: it is ~3.5GB of platform binaries and resolved dependencies.
python3 benchmarks/agent-env/prepare.py --export

# After every run, before believing a number.
python3 benchmarks/agent-env/audit.py jobs/<job>
```

Both gates exit nonzero on failure, so they chain. `run-arm.sh` does the whole
sequence for one arm — wipe, deploy, export, preflight, score, audit:

```sh
./benchmarks/agent-env/run-arm.sh terraform
```

**Export after the deploy, not before.** Deploying an estate writes state into
the arm directory — `terraform.tfstate`, the Pulumi stack, `.alchemy/` — and
trials mount the export rather than that directory. Exporting first hands the
agent a state file from the previous estate, or an empty one, and the arm then
fails or answers about resources that no longer exist. `run-arm.sh` re-exports
between deploy and preflight for this reason.

`preflight.py` checks the emulator first and says so when it is empty, because a
wiped estate fails the same smoke commands a broken tool does and the two should
not read alike.

## How a trial picks the arm up

`aws_bench/agents/claude_code.py` stands the workspace up as root before the
task, driven by three agent kwargs. Nothing happens unless they are given, so
ordinary tasks are unaffected.

| `--ak` kwarg | Meaning |
|---|---|
| `toolchain` | where the exported toolchain is mounted; its binaries are symlinked onto `PATH` |
| `arm_src` | where the exported arm workspace is mounted, read-only |
| `arm_workdir` | the writable path it is copied to, matching the arm's briefing |

Agent kwargs rather than `--agent-env`: the setup runs as root before the agent
process exists, so a variable set on that process would not reach it. `arm_src`
and `arm_workdir` are validated as a pair, because one without the other leaves
the tooling unavailable — the failure this whole directory exists to prevent.

A missing mount fails the trial rather than degrading it. That is the whole
point: a degraded arm produces rewards that look exactly like real ones.

The symlink is deliberate — each agent command gets a fresh shell, so a `PATH`
exported during setup would not survive to the commands that need it.
