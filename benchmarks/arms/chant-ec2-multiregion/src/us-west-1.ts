// Region: us-west-1. Stack "ec2-multiregion-EC2-ls9fuhb522-us-west-1".
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

export const west1Vpc = new Vpc({
  CidrBlock: "10.0.0.0/16",
  EnableDnsSupport: true,
  EnableDnsHostnames: true,
  Tags: [{ Key: "Name", Value: "ResourcesVpc" }],
});

export const west1PublicSubnet = new Subnet({
  VpcId: west1Vpc.VpcId,
  CidrBlock: "10.0.0.0/24",
  MapPublicIpOnLaunch: true,
  Tags: [{ Key: "Name", Value: "Public" }],
});

export const west1Igw = new InternetGateway({});

export const west1IgwAttachment = new VPCGatewayAttachment({
  VpcId: west1Vpc.VpcId,
  InternetGatewayId: west1Igw.InternetGatewayId,
});

export const west1RouteTable = new RouteTable({ VpcId: west1Vpc.VpcId });

export const west1DefaultRoute = new EC2Route({
  RouteTableId: west1RouteTable.RouteTableId,
  DestinationCidrBlock: "0.0.0.0/0",
  GatewayId: west1Igw.InternetGatewayId,
});

export const west1SubnetRouteAssoc = new SubnetRouteTableAssociation({
  RouteTableId: west1RouteTable.RouteTableId,
  SubnetId: west1PublicSubnet.SubnetId,
});

export const west1Server = new Instance({
  SubnetId: west1PublicSubnet.SubnetId,
  InstanceType: "t3.micro",
  ImageId: AL2023,
  Tags: [{ Key: "Name", Value: "WebServerInstance" }],
});
