// The ec2-multiregion estate, authored in Alchemy (published async model,
// alchemy@0.93.12). Same topology the chant, Terraform, Pulumi, and CDK arms
// deploy: us-east-1 carries the full estate (public + private subnet, open +
// unused security groups, IAM, a launch template, four instances incl. one on
// the account default VPC); us-west-1 and us-west-2 each carry one public
// server. Stack names and SSM /exports match the scenario contract so the
// benchmark's ground truth resolves untouched.
//
// Instances, the launch template, the instance profile, and region-scoped
// exports come from ./src/aws-extra.ts — the published Alchemy does not ship
// them (its Cloud Control path needs write support Floci does not have).
import alchemy from "alchemy";
import { AccountId, Policy, Role } from "alchemy/aws";
import {
  InternetGateway,
  InternetGatewayAttachment,
  Route,
  RouteTable,
  RouteTableAssociation,
  SecurityGroup,
  SecurityGroupRule,
  Subnet,
  Vpc,
} from "alchemy/aws/ec2";
import {
  DescribeSubnetsCommand,
  DescribeVpcsCommand,
  EC2Client,
} from "@aws-sdk/client-ec2";
import {
  Ec2Instance,
  Ec2LaunchTemplate,
  ExportParameter,
  IamInstanceProfile,
} from "./src/aws-extra.ts";

const AMI = "ami-0f3f13f145e66a0a3";

const app = await alchemy("alchemy-ec2-multiregion", { stage: "bench" });

// SSM /exports — the benchmark's export collector reads these the same as CFN
// exports, keyed by the last path segment.
function exp(id: string, region: string, name: string, value: string) {
  return ExportParameter(id, { region, name: `/exports/${name}`, value });
}

// ── A public-only region (us-west-1, us-west-2): VPC + public subnet + IGW +
// one server with no explicit SG (default SG) — internet-facing but not
// SSH-reachable.
async function publicRegion(alias: string, region: string, stack: string) {
  // SecurityGroupRule and RouteTableAssociation build their EC2 client from the
  // process region, so pin it per regional block alongside the explicit props.
  process.env.AWS_REGION = region;
  const vpc = await Vpc(`${alias}-vpc`, {
    region,
    cidrBlock: "10.0.0.0/16",
    enableDnsSupport: true,
    enableDnsHostnames: true,
    tags: { Name: "ResourcesVpc" },
  });
  const subnet = await Subnet(`${alias}-public`, {
    region,
    vpc,
    cidrBlock: "10.0.0.0/24",
    availabilityZone: `${region}a`,
    mapPublicIpOnLaunch: true,
    tags: { Name: "Public" },
  });
  const igw = await InternetGateway(`${alias}-igw`, { region });
  await InternetGatewayAttachment(`${alias}-igw-attach`, {
    region,
    internetGateway: igw,
    vpc,
  });
  const rt = await RouteTable(`${alias}-rt`, { region, vpc });
  await Route(`${alias}-route`, {
    region,
    routeTable: rt,
    destinationCidrBlock: "0.0.0.0/0",
    target: { internetGateway: igw },
  });
  await RouteTableAssociation(`${alias}-assoc`, { routeTable: rt, subnet });
  const server = await Ec2Instance(`${alias}-server`, {
    region,
    imageId: AMI,
    instanceType: "t3.micro",
    subnetId: subnet.subnetId,
    tags: { Name: "WebServerInstance" },
  });
  await exp(`${alias}-exp-VpcId`, region, `${stack}-VpcId`, vpc.vpcId);
  await exp(`${alias}-exp-InstanceId`, region, `${stack}-InstanceId`, server.instanceId);
  await exp(`${alias}-exp-PublicSubnetId`, region, `${stack}-PublicSubnetId`, subnet.subnetId);
  await exp(`${alias}-exp-NUMEC2Running`, region, `${stack}-NUMEC2Running`, "1");
  await exp(`${alias}-exp-PrivateIPOfInstance`, region, `${stack}-PrivateIPOfInstance`, server.privateIp);
}

await publicRegion("usw1", "us-west-1", "ec2-multiregion-EC2-ls9fuhb522-us-west-1");
await publicRegion("usw2", "us-west-2", "ec2-multiregion-EC2-ls9fuhb522-us-west-2");

// ── us-east-1 (primary) ──────────────────────────────────────────────────────
const REGION = "us-east-1";
const STACK = "ec2-multiregion-EC2-ks84v1fh12-us-east-1";
process.env.AWS_REGION = REGION;

const account = await AccountId();

// Data lookups (not resources): the account default VPC and its subnets.
const lookup = new EC2Client({ region: REGION });
const defVpcId = (
  await lookup.send(
    new DescribeVpcsCommand({ Filters: [{ Name: "is-default", Values: ["true"] }] }),
  )
).Vpcs![0]!.VpcId!;
const defSubnetIds = (
  (
    await lookup.send(
      new DescribeSubnetsCommand({ Filters: [{ Name: "vpc-id", Values: [defVpcId] }] }),
    )
  ).Subnets ?? []
).map((s) => s.SubnetId!);

const vpc = await Vpc("vpc", {
  region: REGION,
  cidrBlock: "10.0.0.0/16",
  enableDnsSupport: true,
  enableDnsHostnames: true,
  tags: { Name: "ResourcesVpc" },
});
const publicSubnet = await Subnet("public", {
  region: REGION,
  vpc,
  cidrBlock: "10.0.0.0/24",
  availabilityZone: "us-east-1a",
  mapPublicIpOnLaunch: true,
  tags: { Name: "Public" },
});
const privateSubnet = await Subnet("private", {
  region: REGION,
  vpc,
  cidrBlock: "10.0.1.0/24",
  availabilityZone: "us-east-1a",
  mapPublicIpOnLaunch: false,
  tags: { Name: "Private" },
});
const igw = await InternetGateway("igw", { region: REGION });
await InternetGatewayAttachment("igw-attach", {
  region: REGION,
  internetGateway: igw,
  vpc,
});
const rt = await RouteTable("rt", { region: REGION, vpc });
await Route("route", {
  region: REGION,
  routeTable: rt,
  destinationCidrBlock: "0.0.0.0/0",
  target: { internetGateway: igw },
});
await RouteTableAssociation("assoc", { routeTable: rt, subnet: publicSubnet });

const webSg = await SecurityGroup("web", {
  region: REGION,
  vpc,
  groupName: `web-${STACK}`,
  description: "Allow specific inbound traffic",
});
await SecurityGroupRule("web-ssh", {
  securityGroup: webSg,
  type: "ingress",
  protocol: "tcp",
  fromPort: 22,
  toPort: 22,
  cidrBlocks: ["0.0.0.0/0"],
  description: "Allow SSH access",
});
await SecurityGroupRule("web-http", {
  securityGroup: webSg,
  type: "ingress",
  protocol: "tcp",
  fromPort: 80,
  toPort: 80,
  cidrBlocks: ["0.0.0.0/0"],
  description: "Allow HTTP access",
});
await SecurityGroupRule("web-https", {
  securityGroup: webSg,
  type: "ingress",
  protocol: "tcp",
  fromPort: 443,
  toPort: 443,
  cidrBlocks: ["0.0.0.0/0"],
  description: "Allow HTTPS access",
});
const unusedSg = await SecurityGroup("unused", {
  region: REGION,
  vpc,
  groupName: `unused-${STACK}`,
  description: "Unused security group",
});

const role = await Role("instance", {
  roleName: `role-${account}-us-east-1`,
  assumeRolePolicy: {
    Version: "2012-10-17",
    Statement: [
      {
        Effect: "Allow",
        Principal: { Service: "ec2.amazonaws.com" },
        Action: "sts:AssumeRole",
      },
    ],
  },
  managedPolicyArns: ["arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"],
  policies: [
    {
      policyName: `policy-${account}-us-east-1`,
      policyDocument: {
        Version: "2012-10-17",
        Statement: [
          {
            Effect: "Deny",
            Action: [
              "ec2:ModifyInstanceAttribute",
              "ec2:ModifyInstanceMetadataOptions",
            ],
            Resource: "*",
          },
        ],
      },
    },
  ],
});
const profile = await IamInstanceProfile("profile", {
  instanceProfileName: `instanceprofile-${account}-us-east-1`,
  roles: [role.roleName],
});

const webServer = await Ec2Instance("webServer", {
  region: REGION,
  imageId: AMI,
  instanceType: "t3.micro",
  subnetId: publicSubnet.subnetId,
  securityGroupIds: [webSg.groupId],
  iamInstanceProfile: profile.instanceProfileName,
  tags: { Name: "WebServerInstance" },
});

const lt = await Ec2LaunchTemplate("lt", {
  region: REGION,
  launchTemplateName: `lt-${account}-us-east-1`,
  launchTemplateData: {
    ImageId: AMI,
    InstanceType: "t3.micro",
    SecurityGroupIds: [webSg.groupId],
    BlockDeviceMappings: [
      { DeviceName: "/dev/xvda", Ebs: { VolumeSize: 8, VolumeType: "gp3" } },
    ],
    MetadataOptions: { HttpTokens: "required" },
  },
});
// Reachable ONLY through the launch template's security group — the hop a raw
// CLI sweep misses.
const ltServer = await Ec2Instance("ltServer", {
  region: REGION,
  subnetId: publicSubnet.subnetId,
  launchTemplate: { launchTemplateId: lt.launchTemplateId, version: "1" },
  tags: { Name: "LaunchTemplateInstance" },
});
const privateServer = await Ec2Instance("privateServer", {
  region: REGION,
  imageId: AMI,
  instanceType: "t3.micro",
  subnetId: privateSubnet.subnetId,
  tags: { Name: "PrivateInstance" },
});
const defaultVpcServer = await Ec2Instance("defaultVpcServer", {
  region: REGION,
  imageId: AMI,
  instanceType: "t3.micro",
  subnetId: defSubnetIds[0],
  tags: { Name: "MyEC2Instance" },
});

// ── us-east-1 export contract (27) ──────────────────────────────────────────
const e = (id: string, name: string, value: string) =>
  exp(id, REGION, `${STACK}-${name}`, value);
await e("exp-PrivateInstanceId", "PrivateInstanceId", privateServer.instanceId);
await e("exp-PrivateInstancePrivateIp", "PrivateInstancePrivateIp", privateServer.privateIp);
await e("exp-RoleName", "RoleName", role.roleName);
await e("exp-VpcId", "VpcId", vpc.vpcId);
await e("exp-RestrictedActionModifyInstanceAttribute", "RestrictedActionModifyInstanceAttribute", "ec2:ModifyInstanceAttribute");
await e("exp-RestrictedActionModifyInstanceMetadataOptions", "RestrictedActionModifyInstanceMetadataOptions", "ec2:ModifyInstanceMetadataOptions");
await e("exp-SecurityGroupId", "SecurityGroupId", webSg.groupId);
await e("exp-UnusedSecurityGroupId", "UnusedSecurityGroupId", unusedSg.groupId);
await e("exp-PublicSubnetId", "PublicSubnetId", publicSubnet.subnetId);
await e("exp-InstanceProfileName", "InstanceProfileName", profile.instanceProfileName);
await e("exp-AMIName", "AMIName", `ami-${account}-us-east-1`);
await e("exp-InstanceId", "InstanceId", webServer.instanceId);
await e("exp-LaunchTemplateInstanceId", "LaunchTemplateInstanceId", ltServer.instanceId);
await e("exp-NUMEC2Running", "NUMEC2Running", "4");
await e("exp-NUMSubnets", "NUMSubnets", "1");
await e("exp-OpenSSHPort", "OpenSSHPort", "22");
await e("exp-OpenHTTPPort", "OpenHTTPPort", "80");
await e("exp-OpenHTTPsPort", "OpenHTTPsPort", "443");
await e("exp-StackName", "StackName", STACK);
await e("exp-StackId", "StackId", STACK);
await e("exp-PrivateIPOfInstanceWithoutLaunchTemplate", "PrivateIPOfInstanceWithoutLaunchTemplate", webServer.privateIp);
await e("exp-PrivateIPOfInstanceWithLaunchTemplate", "PrivateIPOfInstanceWithLaunchTemplate", ltServer.privateIp);
await e("exp-DefaultVpcId", "DefaultVpcId", defVpcId);
await e("exp-DefaultPublicSubnetId1", "DefaultPublicSubnetId1", defSubnetIds[0]);
await e("exp-DefaultPublicSubnetId2", "DefaultPublicSubnetId2", defSubnetIds[1] ?? defSubnetIds[0]);
await e("exp-DefaultVPCInstanceId", "DefaultVPCInstanceId", defaultVpcServer.instanceId);
await e("exp-PrivateIPOfInstanceWithDefaultVpc", "PrivateIPOfInstanceWithDefaultVpc", defaultVpcServer.privateIp);

// ── QA roles (ec2-multiregion-QARoles-us-east-1) — no exports ────────────────
// NOTE: the CDK original also attaches arn:aws:iam::aws:policy/AmazonBedrockFullAccess
// to the readonly + judge roles. Floci does not pre-seed that managed policy, so
// (like the Terraform and Pulumi arms) the attachment is dropped until Floci
// seeds it (floci-io/floci#2033). These QA roles are harness scaffolding the EC2
// tasks never query, and the judge runs on OAuth in emulator mode.
const assumeByAccount = {
  Version: "2012-10-17" as const,
  Statement: [
    {
      Effect: "Allow" as const,
      Principal: { AWS: `arn:aws:iam::${account}:root` },
      Action: "sts:AssumeRole",
    },
  ],
};
const s3Vectors = await Policy("s3vectors", {
  policyName: `S3VectorsReadOnlyAccess-${account}-us-east-1`,
  description: "Read-only access to S3 Vectors operations",
  document: {
    Version: "2012-10-17",
    Statement: [
      {
        Sid: "AllowS3VectorsReadOnlyAccess",
        Effect: "Allow",
        Action: [
          "s3vectors:ListVectors",
          "s3vectors:GetVectors",
          "s3vectors:GetIndex",
          "s3vectors:GetVectorBucket",
          "s3vectors:GetVectorBucketPolicy",
          "s3vectors:ListIndexes",
          "s3vectors:ListTagsForResource",
          "s3vectors:ListVectorBuckets",
          "s3vectors:QueryVectors",
        ],
        Resource: "*",
      },
    ],
  },
});
await Role("qaReadonly", {
  roleName: "QALocalInvocationApplicationRole",
  assumeRolePolicy: assumeByAccount,
  managedPolicyArns: ["arn:aws:iam::aws:policy/ReadOnlyAccess", s3Vectors.arn],
});
await Role("qaAdmin", {
  roleName: "QALocalInvocationApplicationAdmin",
  assumeRolePolicy: assumeByAccount,
  managedPolicyArns: ["arn:aws:iam::aws:policy/AdministratorAccess"],
});
await Role("qaJudge", {
  roleName: "LLMJudgeFullBedrockAccessRole",
  assumeRolePolicy: assumeByAccount,
  managedPolicyArns: ["arn:aws:iam::aws:policy/ReadOnlyAccess"],
});

console.log("DEPLOYED", {
  account,
  regions: ["us-east-1", "us-west-1", "us-west-2"],
  instances: {
    webServer: webServer.instanceId,
    ltServer: ltServer.instanceId,
    privateServer: privateServer.instanceId,
    defaultVpcServer: defaultVpcServer.instanceId,
  },
});

await app.finalize();
