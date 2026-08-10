// Shared pieces for the three per-region stacks. v2's AWSEnvironment carries a
// single region, so the estate is three stacks — one per region — like the CDK
// arm's per-region stacks. Region comes from AWS_REGION at deploy time.
import * as Alchemy from "alchemy";
import * as AWS from "alchemy/AWS";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import { LtInstanceProvider } from "./LtInstance.ts";

export const AMI = "ami-0f3f13f145e66a0a3";

// AWS providers + the arm's custom launch-template-instance provider. The
// custom provider's SDK calls resolve credentials/region/endpoint from the
// same environment layers AWS.providers() exposes.
export const providers = () => {
  const aws = AWS.providers();
  return Layer.mergeAll(aws, LtInstanceProvider().pipe(Layer.provide(aws)));
};

// SSM /exports — the benchmark's export collector reads these the same as CFN
// exports, keyed by the last path segment.
export const exp = (id: string, name: string, value: any) =>
  AWS.SSM.Parameter(id, {
    name: `/exports/${name}`,
    type: "String",
    value,
  });

// A public-only region (us-west-1, us-west-2): VPC + public subnet + IGW + one
// server with no explicit SG (default SG) — internet-facing but not
// SSH-reachable.
export const publicRegionStack = (region: string, stack: string) =>
  Alchemy.Stack(
    `alchemy-effect-ec2-multiregion-${region}`,
    {
      providers: providers(),
      state: Alchemy.localState(),
    },
    Effect.gen(function* () {
      const vpc = yield* AWS.EC2.Vpc("Vpc", {
        cidrBlock: "10.0.0.0/16",
        tags: { Name: "ResourcesVpc" },
      });
      const subnet = yield* AWS.EC2.Subnet("PublicSubnet", {
        vpcId: vpc.vpcId,
        cidrBlock: "10.0.0.0/24",
        availabilityZone: `${region}a`,
        mapPublicIpOnLaunch: true,
        tags: { Name: "Public" },
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
        subnetId: subnet.subnetId,
      });
      const server = yield* AWS.EC2.Instance("WebServer", {
        imageId: AMI,
        instanceType: "t3.micro",
        subnetId: subnet.subnetId,
        tags: { Name: "WebServerInstance" },
      });
      yield* exp("ExpVpcId", `${stack}-VpcId`, vpc.vpcId);
      yield* exp("ExpInstanceId", `${stack}-InstanceId`, server.instanceId);
      yield* exp("ExpPublicSubnetId", `${stack}-PublicSubnetId`, subnet.subnetId);
      yield* exp("ExpNUMEC2Running", `${stack}-NUMEC2Running`, "1");
      yield* exp("ExpPrivateIPOfInstance", `${stack}-PrivateIPOfInstance`, server.privateIpAddress);
      return {
        vpcId: vpc.vpcId,
        instanceId: server.instanceId,
      };
    }),
  );
