// The one resource the estate needs that Alchemy v2 does not ship: an EC2
// instance launched FROM a launch template. v2's native `AWS.EC2.Instance`
// requires imageId + instanceType and attaches security groups directly, and
// its EC2 docs (alchemy.run/aws/compute/ec2) document no launch-template
// mechanism at all; `AWS.AutoScaling.LaunchTemplate` exists as a resource but
// nothing launches a bare instance from it.
//
// Authored per the documented custom-provider guide
// (alchemy.run/infrastructure-as-code/custom-provider): declare Props and
// Attributes, create the Resource type, then implement the lifecycle through
// the typed `LtInstance.Provider.of({...})` constructor, which checks every
// method's inputs and outputs against the resource's own types.
//
// State fidelity: the attributes deliberately echo the launch-template
// REFERENCE and not the security groups it resolves to — the same shape the
// Terraform and Pulumi states give this estate. The ssh-reachable task turns
// on that hop.
import * as ec2 from "@distilled.cloud/aws/ec2";
import * as Effect from "effect/Effect";
import * as Provider from "alchemy/Provider";
import { Resource } from "alchemy/Resource";

export interface LtInstanceProps {
  subnetId: string;
  launchTemplateId: string;
  launchTemplateVersion?: string;
  tags?: Record<string, string>;
}

export interface LtInstanceAttributes {
  instanceId: string;
  privateIpAddress?: string;
  publicIpAddress?: string;
  subnetId: string;
  launchTemplateId: string;
}

export type LtInstance = Resource<
  "AWS.EC2.LaunchTemplateInstance",
  LtInstanceProps,
  LtInstanceAttributes
>;

export const LtInstance = Resource<LtInstance>(
  "AWS.EC2.LaunchTemplateInstance",
);

const toAttributes = (
  instance: ec2.Instance,
  props: LtInstanceProps,
): LtInstanceAttributes => ({
  instanceId: instance.InstanceId!,
  privateIpAddress: instance.PrivateIpAddress,
  publicIpAddress: instance.PublicIpAddress,
  subnetId: props.subnetId,
  launchTemplateId: props.launchTemplateId,
});

const describeOne = (instanceId: string) =>
  ec2
    .describeInstances({ InstanceIds: [instanceId] })
    .pipe(
      Effect.map((d) => d.Reservations?.[0]?.Instances?.[0]),
      Effect.catchTag("InvalidInstanceID.NotFound", () => Effect.undefined),
    );

const waitRunning = Effect.fn(function* (instanceId: string) {
  for (let attempt = 0; attempt < 60; attempt++) {
    const instance = yield* describeOne(instanceId);
    if (instance?.State?.Name === "running") {
      return instance;
    }
    yield* Effect.sleep(1000);
  }
  return yield* Effect.die(
    new Error(`Instance ${instanceId} not running after 60s`),
  );
});

export const LtInstanceProvider = () =>
  Provider.succeed(
    LtInstance,
    LtInstance.Provider.of({
      stables: ["instanceId"],

      // Nothing to census: this type exists only to launch the estate's
      // launch-template instance, and the account-wide sweep already
      // enumerates instances through AWS.EC2.Instance.
      list: () => Effect.succeed([]),

      reconcile: Effect.fn(function* ({ news, output }) {
        // Observe — a live instance recorded in state is authoritative; a
        // terminated one is a tombstone and falls through to a fresh launch.
        if (output?.instanceId) {
          const existing = yield* describeOne(output.instanceId);
          if (existing && existing.State?.Name !== "terminated") {
            return toAttributes(existing, news);
          }
        }

        // Ensure — every prop here is launch-time, so there is no in-place
        // update path; a changed prop replaces the instance.
        const run = yield* ec2.runInstances({
          MinCount: 1,
          MaxCount: 1,
          SubnetId: news.subnetId,
          LaunchTemplate: {
            LaunchTemplateId: news.launchTemplateId,
            Version: news.launchTemplateVersion ?? "1",
          },
          TagSpecifications: news.tags
            ? [
                {
                  ResourceType: "instance",
                  Tags: Object.entries(news.tags).map(([Key, Value]) => ({
                    Key,
                    Value,
                  })),
                },
              ]
            : undefined,
        });
        const instanceId = run.Instances?.[0]?.InstanceId!;
        const instance = yield* waitRunning(instanceId);
        return toAttributes(instance, news);
      }),

      delete: Effect.fn(function* ({ output }) {
        if (output?.instanceId) {
          yield* ec2
            .terminateInstances({ InstanceIds: [output.instanceId] })
            .pipe(
              Effect.catchTag("InvalidInstanceID.NotFound", () => Effect.void),
            );
        }
      }),
    }),
  );
