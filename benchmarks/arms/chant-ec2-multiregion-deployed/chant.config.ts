import type { ChantConfig } from "@intentius/chant";

export default {
  lexicons: ["aws"],
  environments: ["floci"],
  // The four independently-deployed stacks this estate comprises, each built
  // from its own source directory and observed against its live stack.
  stacks: [
    { name: "ec2-multiregion-EC2-ks84v1fh12-us-east-1", src: "us-east-1/src" },
    { name: "ec2-multiregion-EC2-ls9fuhb522-us-west-1", src: "us-west-1/src" },
    { name: "ec2-multiregion-EC2-ls9fuhb522-us-west-2", src: "us-west-2/src" },
    { name: "ec2-multiregion-QARoles-us-east-1", src: "qa-roles/src" },
  ],
  lint: {
    rules: {
      // Deliberate exposure: the scenario tests detection of the open SSH
      // security group, so the estate must genuinely contain it.
      WAW019: "off",
      WAW049: "off",
    },
  },
} satisfies ChantConfig;
