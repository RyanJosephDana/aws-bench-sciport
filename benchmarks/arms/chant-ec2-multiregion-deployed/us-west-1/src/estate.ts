// Region us-west-1 — deployed by chant under the stack name
// "ec2-multiregion-EC2-ls9fuhb522-us-west-1" (the scenario's original name, so
// every consumer of the stack's exports keeps working).
//
// One VPC (10.0.0.0/16) with a single public subnet and one t3.micro instance
// with a public IP.

import {
  Instance,
  InternetGateway,
  EC2Route,
  RouteTable,
  Subnet,
  SubnetRouteTableAssociation,
  Vpc,
  VPCGatewayAttachment,
  Sub,
  AWS,
  stackOutput,
} from "@intentius/chant-lexicon-aws";

const AL2023 = "ami-0f3f13f145e66a0a3";
const STACK = "ec2-multiregion-EC2-ls9fuhb522-us-west-1";

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

export const igw = new InternetGateway({});

export const igwAttachment = new VPCGatewayAttachment({
  VpcId: vpc.VpcId,
  InternetGatewayId: igw.InternetGatewayId,
});

export const routeTable = new RouteTable({ VpcId: vpc.VpcId });

export const defaultRoute = new EC2Route({
  RouteTableId: routeTable.RouteTableId,
  DestinationCidrBlock: "0.0.0.0/0",
  GatewayId: igw.InternetGatewayId,
});

export const subnetRouteAssoc = new SubnetRouteTableAssociation({
  RouteTableId: routeTable.RouteTableId,
  SubnetId: publicSubnet.SubnetId,
});

export const server = new Instance({
  SubnetId: publicSubnet.SubnetId,
  InstanceType: "t3.micro",
  ImageId: AL2023,
  Tags: [{ Key: "Name", Value: "WebServerInstance" }],
});

// The stack's export contract — names must match the scenario's originals
// exactly; the benchmark's ground-truth placeholders resolve against them.
export const vpcId = stackOutput(vpc.VpcId, { exportName: `${STACK}-VpcId` });
export const instanceId = stackOutput(server.InstanceId, { exportName: `${STACK}-InstanceId` });
export const publicSubnetId = stackOutput(publicSubnet.SubnetId, {
  exportName: `${STACK}-PublicSubnetId`,
});
export const numEc2Running = stackOutput("1", {
  lexicon: "aws",
  exportName: `${STACK}-NUMEC2Running`,
});
export const privateIp = stackOutput(server.PrivateIp, {
  exportName: `${STACK}-PrivateIPOfInstance`,
});
export const stackId = stackOutput(Sub`${AWS.StackId}`, {
  lexicon: "aws",
  exportName: `${STACK}-StackId`,
});
