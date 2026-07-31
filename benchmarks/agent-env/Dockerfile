# Toolchain layer for the agent-under-test container.
#
# The dataset's task environment is python:3.13-slim plus the AWS CLI and jq. It
# ships no JavaScript or IaC runtime, so an arm whose CLI needs one could not run
# it at all: the CDK and Alchemy arms of the scenario-1 runs never executed a
# single `cdk` or `alchemy` command, and Terraform's `show -json` was broken.
# Every arm fell back to grepping raw state, which measures jq, not the tool.
#
# Each runtime here is ambient and pinned, so no arm has to vendor a launcher of
# its own and no arm is measured with its tooling missing.
#
# Build (from the repo root):
#   docker build -t awsbench-agent-tools:latest benchmarks/agent-env
#
# The versions match what the arms vendored for the scenario-1 estates, so the
# tool that answers questions is the tool that deployed.

ARG TASK_IMAGE=public.ecr.aws/docker/library/python:3.13-slim
FROM ${TASK_IMAGE}

ARG NODE_VERSION=24.13.1
ARG BUN_VERSION=1.3.6
ARG TERRAFORM_VERSION=1.15.8
ARG PULUMI_VERSION=3.255.0

# Mirrors the dataset's task environment, so preflight proves the tools against
# the same base the agent actually gets rather than a friendlier one.
# git is not in the dataset's task image. It is needed here because chant's
# `lifecycle snapshot` writes its state record to a `chant/lifecycle` orphan
# branch and fails with "git rev-parse HEAD failed" without it, and because the
# agent adapter installs git anyway when plugins are requested.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl unzip less groff jq zip git \
    && rm -rf /var/lib/apt/lists/*

RUN ARCH=$(uname -m) && \
    if [ "$ARCH" = "aarch64" ] || [ "$ARCH" = "arm64" ]; then \
        CLI_URL="https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip"; \
    else \
        CLI_URL="https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip"; \
    fi && \
    curl -sSL "$CLI_URL" -o /tmp/awscliv2.zip && unzip -q /tmp/awscliv2.zip -d /tmp && \
    /tmp/aws/install && rm -rf /tmp/aws /tmp/awscliv2.zip

# .tar.gz rather than the usual .tar.xz: the slim image has no xz-utils.
RUN ARCH=$(uname -m) && case "$ARCH" in aarch64) N=arm64;; x86_64) N=x64;; esac && \
    curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${N}.tar.gz" -o /tmp/node.tar.gz && \
    tar -xzf /tmp/node.tar.gz -C /opt && mv "/opt/node-v${NODE_VERSION}-linux-${N}" /opt/node && rm /tmp/node.tar.gz

# Alchemy's own runbook drives the program with `bun alchemy.run.ts`.
RUN ARCH=$(uname -m) && case "$ARCH" in aarch64) B=aarch64;; x86_64) B=x64;; esac && \
    curl -fsSL "https://github.com/oven-sh/bun/releases/download/bun-v${BUN_VERSION}/bun-linux-${B}.zip" -o /tmp/bun.zip && \
    unzip -q /tmp/bun.zip -d /tmp && mkdir -p /opt/bun/bin && \
    mv "/tmp/bun-linux-${B}/bun" /opt/bun/bin/bun && rm -rf /tmp/bun.zip "/tmp/bun-linux-${B}"

RUN ARCH=$(uname -m) && case "$ARCH" in aarch64) T=arm64;; x86_64) T=amd64;; esac && \
    curl -fsSL "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_${T}.zip" -o /tmp/tf.zip && \
    unzip -q /tmp/tf.zip -d /opt/terraform-bin && rm /tmp/tf.zip

RUN ARCH=$(uname -m) && case "$ARCH" in aarch64) P=arm64;; x86_64) P=x64;; esac && \
    curl -fsSL "https://get.pulumi.com/releases/sdk/pulumi-v${PULUMI_VERSION}-linux-${P}.tar.gz" -o /tmp/pulumi.tar.gz && \
    tar -xzf /tmp/pulumi.tar.gz -C /opt && rm /tmp/pulumi.tar.gz

# No global aws-cdk. The CDK arm declares its own CLI in devDependencies, and a
# global one shadows it on the bare `cdk` name: a global 2.1112.0 against the
# arm's aws-cdk-lib 2.262.0 fails with a cloud-assembly schema mismatch that
# prints under a wall of synth warnings. Each arm uses the CDK it ships, via
# `npx cdk`.

ENV PATH="/opt/node/bin:/opt/bun/bin:/opt/terraform-bin:/opt/pulumi:${PATH}"
