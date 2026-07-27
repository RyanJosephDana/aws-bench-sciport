// chant-disable WAW019 WAW049 -- deliberate exposure: the benchmark scenario tests detection of the open SSH security group
// Region: us-east-1 (primary). Stack "ec2-multiregion-EC2-ks84v1fh12-us-east-1".
//
// Estate summary for this region:
//   - 1 custom VPC (10.0.0.0/16): public subnet 10.0.0.0/24, isolated private
//     subnet 10.0.1.0/24, internet gateway on the public side, no NAT.
//   - 4 EC2 instances (all t3.micro, Amazon Linux 2023, IMDSv2 required):
//       webServer            public subnet, webSecurityGroup, IAM instance profile
//       launchTemplateServer public subnet, launched from the launch template
//       privateServer        private isolated subnet (no public exposure)
//       defaultVpcServer     the account's DEFAULT VPC (172.31.0.0/16), public subnet
//   - webSecurityGroup opens SSH 22, HTTP 80, HTTPS 443 to 0.0.0.0/0.
//   - unusedSecurityGroup is attached to nothing (deliberately unused).
//   - One AMI ("ami-<account>-us-east-1") baked from webServer at deploy time.

import {
  Instance,
  InternetGateway,
  LaunchTemplate,
  EC2Route,
  RouteTable,
  Role,
  InstanceProfile,
  SecurityGroup,
  Subnet,
  SubnetRouteTableAssociation,
  Vpc,
  VPCGatewayAttachment,
  Sub,
  Ref,
  AWS,
} from "@intentius/chant-lexicon-aws";

const AL2023 = "ami-0f3f13f145e66a0a3"; // Amazon Linux 2023 (resolved at deploy)
const T3_MICRO = "t3.micro";

export const vpc = new Vpc({
  CidrBlock: "10.0.0.0/16",
  EnableDnsSupport: true,
  EnableDnsHostnames: true,
  Tags: [{ Key: "Name", Value: "ResourcesVpc" }],
});

export const publicSubnet = new Subnet({
  VpcId: vpc.VpcId,
  CidrBlock: "10.0.0.0/24",
  MapPublicIpOnLaunch: true,
  Tags: [{ Key: "Name", Value: "Public" }],
});

export const privateSubnet = new Subnet({
  VpcId: vpc.VpcId,
  CidrBlock: "10.0.1.0/24",
  MapPublicIpOnLaunch: false,
  Tags: [{ Key: "Name", Value: "Private" }],
});

export const igw = new InternetGateway({});

export const igwAttachment = new VPCGatewayAttachment({
  VpcId: vpc.VpcId,
  InternetGatewayId: igw.InternetGatewayId,
});

export const publicRouteTable = new RouteTable({ VpcId: vpc.VpcId });

export const publicDefaultRoute = new EC2Route({
  RouteTableId: publicRouteTable.RouteTableId,
  DestinationCidrBlock: "0.0.0.0/0",
  GatewayId: igw.InternetGatewayId,
});

export const publicSubnetRouteAssoc = new SubnetRouteTableAssociation({
  RouteTableId: publicRouteTable.RouteTableId,
  SubnetId: publicSubnet.SubnetId,
});

// SSH, HTTP, and HTTPS open to the world — this is what makes webServer the
// answer to "which instance is reachable from the internet".
export const webSecurityGroup = new SecurityGroup({
  GroupDescription: "Allow specific inbound traffic",
  VpcId: vpc.VpcId,
  SecurityGroupIngress: [
    { IpProtocol: "tcp", FromPort: 22, ToPort: 22, CidrIp: "0.0.0.0/0", Description: "Allow SSH access" },
    { IpProtocol: "tcp", FromPort: 80, ToPort: 80, CidrIp: "0.0.0.0/0", Description: "Allow HTTP access" },
    { IpProtocol: "tcp", FromPort: 443, ToPort: 443, CidrIp: "0.0.0.0/0", Description: "Allow HTTPS access" },
  ],
});

// Attached to nothing, on purpose.
export const unusedSecurityGroup = new SecurityGroup({
  GroupDescription: "Unused security group",
  VpcId: vpc.VpcId,
});

export const instanceRole = new Role({
  RoleName: Sub`role-${AWS.AccountId}-${AWS.Region}`,
  AssumeRolePolicyDocument: {
    Version: "2012-10-17",
    Statement: [
      { Effect: "Allow", Principal: { Service: "ec2.amazonaws.com" }, Action: "sts:AssumeRole" },
    ],
  },
  ManagedPolicyArns: ["arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"],
  Policies: [
    {
      PolicyName: Sub`policy-${AWS.AccountId}-${AWS.Region}`,
      PolicyDocument: {
        Version: "2012-10-17",
        Statement: [
          {
            Effect: "Deny",
            Action: ["ec2:ModifyInstanceAttribute", "ec2:ModifyInstanceMetadataOptions"],
            Resource: "*",
          },
        ],
      },
    },
  ],
});

export const instanceProfile = new InstanceProfile({
  InstanceProfileName: Sub`instanceprofile-${AWS.AccountId}-${AWS.Region}`,
  Roles: [Ref(instanceRole)],
});

export const webServer = new Instance({
  SubnetId: publicSubnet.SubnetId,
  SecurityGroupIds: [webSecurityGroup.GroupId],
  InstanceType: T3_MICRO,
  ImageId: AL2023,
  IamInstanceProfile: Ref(instanceProfile),
  Tags: [{ Key: "Name", Value: "WebServerInstance" }],
});

export const launchTemplate = new LaunchTemplate({
  LaunchTemplateName: Sub`lt-${AWS.AccountId}-${AWS.Region}`,
  LaunchTemplateData: {
    InstanceType: T3_MICRO,
    ImageId: AL2023,
    SecurityGroupIds: [webSecurityGroup.GroupId],
    BlockDeviceMappings: [
      { DeviceName: "/dev/xvda", Ebs: { VolumeSize: 8, VolumeType: "gp3" } },
    ],
    MetadataOptions: { HttpTokens: "required" },
  },
});

export const launchTemplateServer = new Instance({
  LaunchTemplate: { LaunchTemplateId: launchTemplate.LaunchTemplateId, Version: "1" },
  SubnetId: publicSubnet.SubnetId,
  Tags: [{ Key: "Name", Value: "LaunchTemplateInstance" }],
});

// Private isolated subnet — must NOT appear in public-reachability answers.
export const privateServer = new Instance({
  SubnetId: privateSubnet.SubnetId,
  InstanceType: T3_MICRO,
  ImageId: AL2023,
  Tags: [{ Key: "Name", Value: "PrivateInstance" }],
});

// Lives in the account's DEFAULT VPC (172.31.0.0/16), not the custom VPC.
// The subnet id resolves to the default VPC's first public subnet at deploy.
export const defaultVpcServer = new Instance({
  InstanceType: T3_MICRO,
  ImageId: AL2023,
  Tags: [
    { Key: "Name", Value: "MyEC2Instance" },
    { Key: "chant:placement", Value: "default-vpc-public-subnet" },
  ],
});

// Deploy-time bake: an AMI named `ami-<account>-us-east-1` is created from
// webServer (EC2 CreateImage) as part of this region's rollout.
