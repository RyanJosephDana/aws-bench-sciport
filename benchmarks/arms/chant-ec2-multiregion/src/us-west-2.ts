// Region: us-west-2. Stack "ec2-multiregion-EC2-ls9fuhb522-us-west-2".
//
// Estate summary for this region: one VPC (10.0.0.0/16) with a single public
// subnet (10.0.0.0/24) and one t3.micro instance with a public IP. No NAT,
// no private subnets, no extra security groups.

import {
  Instance,
  InternetGateway,
  EC2Route,
  RouteTable,
  Subnet,
  SubnetRouteTableAssociation,
  Vpc,
  VPCGatewayAttachment,
} from "@intentius/chant-lexicon-aws";

const AL2023 = "ami-0f3f13f145e66a0a3";

export const west2Vpc = new Vpc({
  CidrBlock: "10.0.0.0/16",
  EnableDnsSupport: true,
  EnableDnsHostnames: true,
  Tags: [{ Key: "Name", Value: "ResourcesVpc" }],
});

export const west2PublicSubnet = new Subnet({
  VpcId: west2Vpc.VpcId,
  CidrBlock: "10.0.0.0/24",
  MapPublicIpOnLaunch: true,
  Tags: [{ Key: "Name", Value: "Public" }],
});

export const west2Igw = new InternetGateway({});

export const west2IgwAttachment = new VPCGatewayAttachment({
  VpcId: west2Vpc.VpcId,
  InternetGatewayId: west2Igw.InternetGatewayId,
});

export const west2RouteTable = new RouteTable({ VpcId: west2Vpc.VpcId });

export const west2DefaultRoute = new EC2Route({
  RouteTableId: west2RouteTable.RouteTableId,
  DestinationCidrBlock: "0.0.0.0/0",
  GatewayId: west2Igw.InternetGatewayId,
});

export const west2SubnetRouteAssoc = new SubnetRouteTableAssociation({
  RouteTableId: west2RouteTable.RouteTableId,
  SubnetId: west2PublicSubnet.SubnetId,
});

export const west2Server = new Instance({
  SubnetId: west2PublicSubnet.SubnetId,
  InstanceType: "t3.micro",
  ImageId: AL2023,
  Tags: [{ Key: "Name", Value: "WebServerInstance" }],
});
