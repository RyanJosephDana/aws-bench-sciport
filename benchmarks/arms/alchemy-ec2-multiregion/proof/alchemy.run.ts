// Floci endpoint-override proof (published async Alchemy 0.93.12 + local patch to
// lib/aws/ec2/utils.js honoring AWS_ENDPOINT_URL). Deploys one VPC + one subnet to
// the local emulator. Confirms the patched provider can target Floci — the gate
// for the ec2-multiregion benchmark arm.
import alchemy from "alchemy";
import { Vpc, Subnet } from "alchemy/aws/ec2";

const app = await alchemy("alchemy-floci-proof");

const vpc = await Vpc("ProofVpc", { cidrBlock: "10.77.0.0/16" });
const subnet = await Subnet("ProofSubnet", {
  vpc,
  cidrBlock: "10.77.1.0/24",
  availabilityZone: "us-east-1a",
});

console.log("DEPLOYED vpcId:", vpc.vpcId, "subnetId:", subnet.subnetId);

await app.finalize();
