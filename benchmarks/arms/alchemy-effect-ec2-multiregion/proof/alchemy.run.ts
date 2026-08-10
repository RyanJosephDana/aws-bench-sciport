// Floci endpoint proof for the Alchemy v2 Effect line (2.0.0-beta.65 + the
// env-endpoint patch in ../apply-endpoint-patch.sh). Deploys one VPC + one
// subnet to the local emulator — the gate for the alchemy-effect arm.
import * as Alchemy from "alchemy";
import * as AWS from "alchemy/AWS";
import * as Effect from "effect/Effect";

export default Alchemy.Stack(
  "alchemy-effect-proof",
  {
    providers: AWS.providers(),
    state: Alchemy.localState(),
  },
  Effect.gen(function* () {
    const vpc = yield* AWS.EC2.Vpc("ProofVpc", {
      cidrBlock: "10.88.0.0/16",
      tags: { Name: "ProofVpc" },
    });
    const subnet = yield* AWS.EC2.Subnet("ProofSubnet", {
      vpcId: vpc.vpcId,
      cidrBlock: "10.88.1.0/24",
      availabilityZone: "us-east-1a",
    });
    return { vpcId: vpc.vpcId, subnetId: subnet.subnetId };
  }),
);
