// The one resource the estate needs that Alchemy v2 does not ship: an EC2
// instance launched FROM a launch template. v2's native `AWS.EC2.Instance`
// requires imageId + instanceType and attaches security groups directly;
// `AWS.AutoScaling.LaunchTemplate` exists but nothing launches a bare
// instance from it. Authored in the framework's own provider style
// (Provider.effect + reconcile/delete on the @distilled.cloud/aws SDK).
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

export interface LtInstance
  extends Resource<
    "AWS.EC2.LaunchTemplateInstance",
    LtInstanceProps,
    LtInstanceAttributes
  > {}

export const LtInstance = Resource<LtInstance>(
  "AWS.EC2.LaunchTemplateInstance",
);

const toAttributes = (
  instance: ec2.Instance,
  news: LtInstanceProps,
): LtInstanceAttributes => ({
  instanceId: instance.InstanceId!,
  privateIpAddress: instance.PrivateIpAddress,
  publicIpAddress: instance.PublicIpAddress,
  subnetId: news.subnetId,
  launchTemplateId: news.launchTemplateId,
});

const waitRunning = (instanceId: string) =>
  Effect.gen(function* () {
    for (let attempt = 0; attempt < 60; attempt++) {
      const described = yield* ec2.describeInstances({
        InstanceIds: [instanceId],
      });
      const instance = described.Reservations?.[0]?.Instances?.[0];
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
  Provider.effect(
    LtInstance,
    Effect.gen(function* () {
      return {
        stables: ["instanceId"],

        list: () => Effect.succeed([]),

        reconcile: Effect.fn(function* ({ news, output }) {
          if (output?.instanceId) {
            const described = yield* ec2
              .describeInstances({ InstanceIds: [output.instanceId] })
              .pipe(
                Effect.catchTag("InvalidInstanceID.NotFound", () =>
                  Effect.succeed({ Reservations: [] }),
                ),
              );
            const existing = described.Reservations?.[0]?.Instances?.[0];
            if (existing && existing.State?.Name !== "terminated") {
              return toAttributes(existing, news as LtInstanceProps);
            }
          }
          const props = news as LtInstanceProps;
          const run = yield* ec2.runInstances({
            MinCount: 1,
            MaxCount: 1,
            SubnetId: props.subnetId,
            LaunchTemplate: {
              LaunchTemplateId: props.launchTemplateId,
              Version: props.launchTemplateVersion ?? "1",
            },
            TagSpecifications: props.tags
              ? [
                  {
                    ResourceType: "instance",
                    Tags: Object.entries(props.tags).map(([Key, Value]) => ({
                      Key,
                      Value,
                    })),
                  },
                ]
              : undefined,
          });
          const instanceId = run.Instances?.[0]?.InstanceId!;
          const instance = yield* waitRunning(instanceId);
          return toAttributes(instance, props);
        }),

        delete: Effect.fn(function* ({ output }) {
          if (output?.instanceId) {
            yield* ec2
              .terminateInstances({ InstanceIds: [output.instanceId] })
              .pipe(
                Effect.catchTag("InvalidInstanceID.NotFound", () =>
                  Effect.void,
                ),
              );
          }
        }),
      };
    }),
  );
