// Resources the published Alchemy (0.93.12, async model) does not ship:
// EC2 Instance, EC2 Launch Template, IAM Instance Profile, and a region-scoped
// SSM parameter (Alchemy's SSMParameter builds its client with no region, so it
// can only write to the process region). Authored the same way the framework's
// own AWS resources are — an async lifecycle function per resource — but on AWS
// SDK v3 clients, which honor AWS_ENDPOINT_URL natively (no endpoint patch).
//
// Type strings follow the framework's own convention for these services —
// `aws::Instance` / `aws::LaunchTemplate` alongside the native `aws::Vpc` and
// `aws::Subnet`, `iam::InstanceProfile` alongside `iam::Role`. The regional
// SSM parameter is `ssm::RegionalParameter` because `ssm::Parameter` is the
// native (process-region-only) resource's own type.
//
// The Instance echoes only its DECLARED props into state plus runtime ids/IPs.
// In particular an instance launched via launch template records the template
// reference, not the security groups it resolves to — the same shape Terraform
// and Pulumi state gives this estate, which is what the ssh-reachable task
// turns on.
import { Resource, type Context } from "alchemy";
import {
  CreateLaunchTemplateCommand,
  DeleteLaunchTemplateCommand,
  DescribeInstancesCommand,
  EC2Client,
  RunInstancesCommand,
  TerminateInstancesCommand,
  type RequestLaunchTemplateData,
} from "@aws-sdk/client-ec2";
import {
  AddRoleToInstanceProfileCommand,
  CreateInstanceProfileCommand,
  DeleteInstanceProfileCommand,
  IAMClient,
  RemoveRoleFromInstanceProfileCommand,
} from "@aws-sdk/client-iam";
import {
  DeleteParameterCommand,
  PutParameterCommand,
  SSMClient,
} from "@aws-sdk/client-ssm";

export interface Ec2InstanceProps {
  region: string;
  subnetId: string;
  imageId?: string;
  instanceType?: string;
  securityGroupIds?: string[];
  iamInstanceProfile?: string;
  launchTemplate?: { launchTemplateId: string; version?: string };
  tags?: Record<string, string>;
}

export interface Ec2Instance extends Ec2InstanceProps {
  instanceId: string;
  privateIp: string;
  publicIp?: string;
}

export const Ec2Instance = Resource(
  "aws::Instance",
  async function (
    this: Context<Ec2Instance>,
    _id: string,
    props: Ec2InstanceProps,
  ): Promise<Ec2Instance> {
    const ec2 = new EC2Client({ region: props.region });

    if (this.phase === "delete") {
      if (this.output?.instanceId) {
        try {
          await ec2.send(
            new TerminateInstancesCommand({
              InstanceIds: [this.output.instanceId],
            }),
          );
        } catch (error: any) {
          if (!String(error.name).startsWith("InvalidInstanceID")) {
            throw error;
          }
        }
      }
      return this.destroy();
    }

    if (this.phase === "update") {
      // Every prop here is launch-time; any change means a new instance.
      return this.replace();
    }

    const run = await ec2.send(
      new RunInstancesCommand({
        MinCount: 1,
        MaxCount: 1,
        ImageId: props.imageId,
        InstanceType: props.instanceType as any,
        SubnetId: props.subnetId,
        SecurityGroupIds: props.securityGroupIds,
        IamInstanceProfile: props.iamInstanceProfile
          ? { Name: props.iamInstanceProfile }
          : undefined,
        LaunchTemplate: props.launchTemplate
          ? {
              LaunchTemplateId: props.launchTemplate.launchTemplateId,
              Version: props.launchTemplate.version ?? "1",
            }
          : undefined,
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
      }),
    );
    const instanceId = run.Instances![0]!.InstanceId!;

    let privateIp: string | undefined;
    let publicIp: string | undefined;
    for (let attempt = 0; ; attempt++) {
      const described = await ec2.send(
        new DescribeInstancesCommand({ InstanceIds: [instanceId] }),
      );
      const instance = described.Reservations?.[0]?.Instances?.[0];
      if (instance?.State?.Name === "running") {
        privateIp = instance.PrivateIpAddress;
        publicIp = instance.PublicIpAddress;
        break;
      }
      if (attempt >= 60) {
        throw new Error(`Instance ${instanceId} not running after 60s`);
      }
      await new Promise((resolve) => setTimeout(resolve, 1000));
    }

    return { ...props, instanceId, privateIp: privateIp!, publicIp };
  },
);

export interface Ec2LaunchTemplateProps {
  region: string;
  launchTemplateName: string;
  launchTemplateData: RequestLaunchTemplateData;
}

export interface Ec2LaunchTemplate extends Ec2LaunchTemplateProps {
  launchTemplateId: string;
}

export const Ec2LaunchTemplate = Resource(
  "aws::LaunchTemplate",
  async function (
    this: Context<Ec2LaunchTemplate>,
    _id: string,
    props: Ec2LaunchTemplateProps,
  ): Promise<Ec2LaunchTemplate> {
    const ec2 = new EC2Client({ region: props.region });

    if (this.phase === "delete") {
      if (this.output?.launchTemplateId) {
        try {
          await ec2.send(
            new DeleteLaunchTemplateCommand({
              LaunchTemplateId: this.output.launchTemplateId,
            }),
          );
        } catch (error: any) {
          if (!String(error.name).startsWith("InvalidLaunchTemplateId")) {
            throw error;
          }
        }
      }
      return this.destroy();
    }

    if (this.phase === "update") {
      return this.replace();
    }

    const created = await ec2.send(
      new CreateLaunchTemplateCommand({
        LaunchTemplateName: props.launchTemplateName,
        LaunchTemplateData: props.launchTemplateData,
      }),
    );

    return {
      ...props,
      launchTemplateId: created.LaunchTemplate!.LaunchTemplateId!,
    };
  },
);

export interface IamInstanceProfileProps {
  instanceProfileName: string;
  roles: string[];
}

export interface IamInstanceProfile extends IamInstanceProfileProps {
  arn: string;
}

export const IamInstanceProfile = Resource(
  "iam::InstanceProfile",
  async function (
    this: Context<IamInstanceProfile>,
    _id: string,
    props: IamInstanceProfileProps,
  ): Promise<IamInstanceProfile> {
    const iam = new IAMClient({});

    if (this.phase === "delete") {
      const name = this.output?.instanceProfileName;
      if (name) {
        for (const role of this.output?.roles ?? []) {
          try {
            await iam.send(
              new RemoveRoleFromInstanceProfileCommand({
                InstanceProfileName: name,
                RoleName: role,
              }),
            );
          } catch (error: any) {
            if (error.name !== "NoSuchEntityException") throw error;
          }
        }
        try {
          await iam.send(
            new DeleteInstanceProfileCommand({ InstanceProfileName: name }),
          );
        } catch (error: any) {
          if (error.name !== "NoSuchEntityException") throw error;
        }
      }
      return this.destroy();
    }

    if (this.phase === "update") {
      return this.replace();
    }

    const created = await iam.send(
      new CreateInstanceProfileCommand({
        InstanceProfileName: props.instanceProfileName,
      }),
    );
    for (const role of props.roles) {
      await iam.send(
        new AddRoleToInstanceProfileCommand({
          InstanceProfileName: props.instanceProfileName,
          RoleName: role,
        }),
      );
    }

    return { ...props, arn: created.InstanceProfile!.Arn! };
  },
);

export interface ExportParameterProps {
  region: string;
  name: string;
  value: string;
}

export type ExportParameter = ExportParameterProps;

export const ExportParameter = Resource(
  "ssm::RegionalParameter",
  async function (
    this: Context<ExportParameter>,
    _id: string,
    props: ExportParameterProps,
  ): Promise<ExportParameter> {
    const ssm = new SSMClient({ region: props.region });

    if (this.phase === "delete") {
      if (this.output?.name) {
        try {
          await ssm.send(new DeleteParameterCommand({ Name: this.output.name }));
        } catch (error: any) {
          if (error.name !== "ParameterNotFound") throw error;
        }
      }
      return this.destroy();
    }

    await ssm.send(
      new PutParameterCommand({
        Name: props.name,
        Type: "String",
        Value: props.value,
        Overwrite: true,
      }),
    );

    return { ...props };
  },
);
