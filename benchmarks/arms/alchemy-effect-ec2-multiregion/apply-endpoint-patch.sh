#!/usr/bin/env bash
# Point Alchemy v2 (Effect line, 2.0.0-beta.65) at a local AWS endpoint (Floci).
#
# v2 already has a first-class endpoint layer — every SDK call resolves its
# endpoint from AWSEnvironment.endpoint via Endpoint.fromEnvironment. The one
# gap: nothing populates that field. The env-var auth path (CI=true +
# AWS_ACCESS_KEY_ID/...) resolves account, region, and credentials from the
# environment but drops the endpoint, so the SDK falls back to real AWS URLs.
#
# This patches the env auth branch to carry AWS_ENDPOINT_URL into the resolved
# environment. One logical change, applied to both module flavors (bun's
# export condition loads src/*.ts; node loads lib/*.js).
#
# Two Floci-compat fixes ride along:
#
#   1. The InstanceProfile provider stamps its ownership tags via the separate
#      TagInstanceProfile API, which Floci does not implement ("Operation
#      TagInstanceProfile is not supported"). The patch makes that tagging
#      call tolerate the unsupported operation — the profile itself creates
#      fine, only the tag stamp is skipped.
#   2. The Instance provider launches through a primary NetworkInterfaces[0]
#      spec whenever subnetId is set. Floci ignores NetworkInterfaces on
#      RunInstances, silently dropping the subnet placement AND the security
#      groups (instances land in the default subnet with the default SG). The
#      patch narrows the primary-NI path to the cases that genuinely need it
#      (associatePublicIpAddress / privateIpAddress) so plain subnet + SG
#      launches use the top-level RunInstances fields — equally valid against
#      real AWS.
set -euo pipefail
cd "$(dirname "$0")"

python3 - <<'PY'
sites = [
    (
        "node_modules/alchemy/src/AWS/AuthProvider.ts",
        """                region,
                source: { type: "env" as const },
              } satisfies AwsResolvedCredentials;""",
        """                region,
                endpoint: process.env.AWS_ENDPOINT_URL || undefined,
                source: { type: "env" as const },
              } as AwsResolvedCredentials;""",
    ),
    (
        "node_modules/alchemy/lib/AWS/AuthProvider.js",
        """            region,
            source: { type: "env" },
        };""",
        """            region,
            endpoint: process.env.AWS_ENDPOINT_URL || undefined,
            source: { type: "env" },
        };""",
    ),
    (
        "node_modules/alchemy/src/AWS/IAM/InstanceProfile.ts",
        """            yield* iam.tagInstanceProfile({
              InstanceProfileName: name,
              Tags: upsert,
            });""",
        """            yield* iam.tagInstanceProfile({
              InstanceProfileName: name,
              Tags: upsert,
            }).pipe(Effect.catchTag("UnknownAwsError", () => Effect.void));""",
    ),
    (
        "node_modules/alchemy/lib/AWS/IAM/InstanceProfile.js",
        """                yield* iam.tagInstanceProfile({
                    InstanceProfileName: name,
                    Tags: upsert,
                });""",
        """                yield* iam.tagInstanceProfile({
                    InstanceProfileName: name,
                    Tags: upsert,
                }).pipe(Effect.catchTag("UnknownAwsError", () => Effect.void));""",
    ),
    (
        "node_modules/alchemy/src/AWS/EC2/hosted.ts",
        """    const usePrimaryNetworkInterface =
      subnetId !== undefined ||
      associatePublicIpAddress !== undefined ||
      privateIpAddress !== undefined;""",
        """    const usePrimaryNetworkInterface =
      associatePublicIpAddress !== undefined ||
      privateIpAddress !== undefined;""",
    ),
    (
        "node_modules/alchemy/lib/AWS/EC2/hosted.js",
        """        const usePrimaryNetworkInterface = subnetId !== undefined ||
            associatePublicIpAddress !== undefined ||
            privateIpAddress !== undefined;""",
        """        const usePrimaryNetworkInterface = associatePublicIpAddress !== undefined ||
            privateIpAddress !== undefined;""",
    ),
]
for path, old, new in sites:
    s = open(path, encoding="utf-8").read()
    if new in s:
        print("already patched:", path)
        continue
    assert old in s, f"patch site not found in {path} — Alchemy version drift, re-locate it"
    open(path, "w", encoding="utf-8").write(s.replace(old, new, 1))
    print("patched:", path)
PY
