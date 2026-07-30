// us-east-1 (primary): full estate — public + private subnet, open + unused
// security groups, IAM, a launch template, four instances incl. one on the
// account default VPC, the 27-export SSM contract, and the QA roles. Stack
// names and /exports keys match the scenario contract so the benchmark's
// ground truth resolves untouched.
import {
  DescribeSubnetsCommand,
  DescribeVpcsCommand,
  EC2Client,
} from "@aws-sdk/client-ec2";
import * as Alchemy from "alchemy";
import * as AWS from "alchemy/AWS";
import * as Effect from "effect/Effect";
import { LtInstance } from "./src/LtInstance.ts";
import { AMI, exp, providers } from "./src/shared.ts";

const STACK = "ec2-multiregion-EC2-ks84v1fh12-us-east-1";

// Data lookups (not resources): the account default VPC and its subnets, via
// the AWS SDK, which honors AWS_ENDPOINT_URL natively.
const defaultVpcLookup = Effect.promise(async () => {
  const client = new EC2Client({ region: "us-east-1" });
  const vpcs = await client.send(
    new DescribeVpcsCommand({
      Filters: [{ Name: "is-default", Values: ["true"] }],
    }),
  );
  const vpcId = vpcs.Vpcs![0]!.VpcId!;
  const subnets = await client.send(
    new DescribeSubnetsCommand({
      Filters: [{ Name: "vpc-id", Values: [vpcId] }],
    }),
  );
  return { vpcId, subnetIds: (subnets.Subnets ?? []).map((s) => s.SubnetId!) };
});

export default Alchemy.Stack(
  "alchemy-effect-ec2-multiregion-us-east-1",
  {
    providers: providers(),
    state: Alchemy.localState(),
  },
  Effect.gen(function* () {
    const { accountId } = yield* AWS.AWSEnvironment.current;
    const def = yield* defaultVpcLookup;

    const vpc = yield* AWS.EC2.Vpc("Vpc", {
      cidrBlock: "10.0.0.0/16",
      tags: { Name: "ResourcesVpc" },
    });
    const publicSubnet = yield* AWS.EC2.Subnet("PublicSubnet", {
      vpcId: vpc.vpcId,
      cidrBlock: "10.0.0.0/24",
      availabilityZone: "us-east-1a",
      mapPublicIpOnLaunch: true,
      tags: { Name: "Public" },
    });
    const privateSubnet = yield* AWS.EC2.Subnet("PrivateSubnet", {
      vpcId: vpc.vpcId,
      cidrBlock: "10.0.1.0/24",
      availabilityZone: "us-east-1a",
      mapPublicIpOnLaunch: false,
      tags: { Name: "Private" },
    });
    const igw = yield* AWS.EC2.InternetGateway("InternetGateway", {
      vpcId: vpc.vpcId,
    });
    const routeTable = yield* AWS.EC2.RouteTable("PublicRouteTable", {
      vpcId: vpc.vpcId,
    });
    yield* AWS.EC2.Route("InternetRoute", {
      routeTableId: routeTable.routeTableId,
      destinationCidrBlock: "0.0.0.0/0",
      gatewayId: igw.internetGatewayId,
    });
    yield* AWS.EC2.RouteTableAssociation("PublicAssociation", {
      routeTableId: routeTable.routeTableId,
      subnetId: publicSubnet.subnetId,
    });

    const webSg = yield* AWS.EC2.SecurityGroup("WebSecurityGroup", {
      vpcId: vpc.vpcId,
      groupName: `web-${STACK}`,
      description: "Allow specific inbound traffic",
      ingress: [
        {
          ipProtocol: "tcp",
          fromPort: 22,
          toPort: 22,
          cidrIpv4: "0.0.0.0/0",
          description: "Allow SSH access",
        },
        {
          ipProtocol: "tcp",
          fromPort: 80,
          toPort: 80,
          cidrIpv4: "0.0.0.0/0",
          description: "Allow HTTP access",
        },
        {
          ipProtocol: "tcp",
          fromPort: 443,
          toPort: 443,
          cidrIpv4: "0.0.0.0/0",
          description: "Allow HTTPS access",
        },
      ],
    });
    const unusedSg = yield* AWS.EC2.SecurityGroup("UnusedSecurityGroup", {
      vpcId: vpc.vpcId,
      groupName: `unused-${STACK}`,
      description: "Unused security group",
    });

    const role = yield* AWS.IAM.Role("InstanceRole", {
      roleName: `role-${accountId}-us-east-1`,
      assumeRolePolicyDocument: {
        Version: "2012-10-17",
        Statement: [
          {
            Effect: "Allow",
            Principal: { Service: "ec2.amazonaws.com" },
            Action: "sts:AssumeRole",
          },
        ],
      },
      managedPolicyArns: [
        "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
      ],
      inlinePolicies: {
        [`policy-${accountId}-us-east-1`]: {
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
    });
    const profile = yield* AWS.IAM.InstanceProfile("InstanceProfile", {
      instanceProfileName: `instanceprofile-${accountId}-us-east-1`,
      roleName: role.roleName,
    });

    const webServer = yield* AWS.EC2.Instance("WebServer", {
      imageId: AMI,
      instanceType: "t3.micro",
      subnetId: publicSubnet.subnetId,
      securityGroupIds: [webSg.groupId],
      instanceProfileName: profile.instanceProfileName,
      tags: { Name: "WebServerInstance" },
    });

    const launchTemplate = yield* AWS.AutoScaling.LaunchTemplate(
      "WebLaunchTemplate",
      {
        launchTemplateName: `lt-${accountId}-us-east-1`,
        imageId: AMI,
        instanceType: "t3.micro",
        securityGroupIds: [webSg.groupId],
      },
    );
    // Reachable ONLY through the launch template's security group — the hop a
    // raw CLI sweep misses.
    const ltServer = yield* LtInstance("LaunchTemplateServer", {
      subnetId: publicSubnet.subnetId,
      launchTemplateId: launchTemplate.launchTemplateId,
      launchTemplateVersion: "1",
      tags: { Name: "LaunchTemplateInstance" },
    });
    const privateServer = yield* AWS.EC2.Instance("PrivateServer", {
      imageId: AMI,
      instanceType: "t3.micro",
      subnetId: privateSubnet.subnetId,
      tags: { Name: "PrivateInstance" },
    });
    const defaultVpcServer = yield* AWS.EC2.Instance("DefaultVpcServer", {
      imageId: AMI,
      instanceType: "t3.micro",
      subnetId: def.subnetIds[0] as AWS.EC2.SubnetId,
      tags: { Name: "MyEC2Instance" },
    });

    // ── export contract (27) ────────────────────────────────────────────────
    const e = (id: string, name: string, value: any) =>
      exp(id, `${STACK}-${name}`, value);
    yield* e("ExpPrivateInstanceId", "PrivateInstanceId", privateServer.instanceId);
    yield* e("ExpPrivateInstancePrivateIp", "PrivateInstancePrivateIp", privateServer.privateIpAddress);
    yield* e("ExpRoleName", "RoleName", role.roleName);
    yield* e("ExpVpcId", "VpcId", vpc.vpcId);
    yield* e("ExpRestrictedActionModifyInstanceAttribute", "RestrictedActionModifyInstanceAttribute", "ec2:ModifyInstanceAttribute");
    yield* e("ExpRestrictedActionModifyInstanceMetadataOptions", "RestrictedActionModifyInstanceMetadataOptions", "ec2:ModifyInstanceMetadataOptions");
    yield* e("ExpSecurityGroupId", "SecurityGroupId", webSg.groupId);
    yield* e("ExpUnusedSecurityGroupId", "UnusedSecurityGroupId", unusedSg.groupId);
    yield* e("ExpPublicSubnetId", "PublicSubnetId", publicSubnet.subnetId);
    yield* e("ExpInstanceProfileName", "InstanceProfileName", profile.instanceProfileName);
    yield* e("ExpAMIName", "AMIName", `ami-${accountId}-us-east-1`);
    yield* e("ExpInstanceId", "InstanceId", webServer.instanceId);
    yield* e("ExpLaunchTemplateInstanceId", "LaunchTemplateInstanceId", ltServer.instanceId);
    yield* e("ExpNUMEC2Running", "NUMEC2Running", "4");
    yield* e("ExpNUMSubnets", "NUMSubnets", "1");
    yield* e("ExpOpenSSHPort", "OpenSSHPort", "22");
    yield* e("ExpOpenHTTPPort", "OpenHTTPPort", "80");
    yield* e("ExpOpenHTTPsPort", "OpenHTTPsPort", "443");
    yield* e("ExpStackName", "StackName", STACK);
    yield* e("ExpStackId", "StackId", STACK);
    yield* e("ExpPrivateIPOfInstanceWithoutLaunchTemplate", "PrivateIPOfInstanceWithoutLaunchTemplate", webServer.privateIpAddress);
    yield* e("ExpPrivateIPOfInstanceWithLaunchTemplate", "PrivateIPOfInstanceWithLaunchTemplate", ltServer.privateIpAddress);
    yield* e("ExpDefaultVpcId", "DefaultVpcId", def.vpcId);
    yield* e("ExpDefaultPublicSubnetId1", "DefaultPublicSubnetId1", def.subnetIds[0]);
    yield* e("ExpDefaultPublicSubnetId2", "DefaultPublicSubnetId2", def.subnetIds[1] ?? def.subnetIds[0]);
    yield* e("ExpDefaultVPCInstanceId", "DefaultVPCInstanceId", defaultVpcServer.instanceId);
    yield* e("ExpPrivateIPOfInstanceWithDefaultVpc", "PrivateIPOfInstanceWithDefaultVpc", defaultVpcServer.privateIpAddress);

    // ── QA roles (ec2-multiregion-QARoles-us-east-1) — no exports ───────────
    // NOTE: the CDK original also attaches AmazonBedrockFullAccess to the
    // readonly + judge roles. Floci does not pre-seed that managed policy, so
    // (like every other arm) the attachment is dropped until Floci seeds it
    // (floci-io/floci#2033). Harness scaffolding the EC2 tasks never query.
    const assumeByAccount = {
      Version: "2012-10-17",
      Statement: [
        {
          Effect: "Allow",
          Principal: { AWS: `arn:aws:iam::${accountId}:root` },
          Action: "sts:AssumeRole",
        },
      ],
    };
    const s3Vectors = yield* AWS.IAM.Policy("S3VectorsPolicy", {
      policyName: `S3VectorsReadOnlyAccess-${accountId}-us-east-1`,
      description: "Read-only access to S3 Vectors operations",
      policyDocument: {
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
    yield* AWS.IAM.Role("QaReadonlyRole", {
      roleName: "QALocalInvocationApplicationRole",
      assumeRolePolicyDocument: assumeByAccount,
      managedPolicyArns: [
        "arn:aws:iam::aws:policy/ReadOnlyAccess",
        s3Vectors.policyArn,
      ],
    });
    yield* AWS.IAM.Role("QaAdminRole", {
      roleName: "QALocalInvocationApplicationAdmin",
      assumeRolePolicyDocument: assumeByAccount,
      managedPolicyArns: ["arn:aws:iam::aws:policy/AdministratorAccess"],
    });
    yield* AWS.IAM.Role("QaJudgeRole", {
      roleName: "LLMJudgeFullBedrockAccessRole",
      assumeRolePolicyDocument: assumeByAccount,
      managedPolicyArns: ["arn:aws:iam::aws:policy/ReadOnlyAccess"],
    });

    return {
      webServerId: webServer.instanceId,
      launchTemplateServerId: ltServer.instanceId,
      privateServerId: privateServer.instanceId,
      defaultVpcServerId: defaultVpcServer.instanceId,
    };
  }),
);
