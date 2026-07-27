import type { ChantConfig } from "@intentius/chant";

export default {
  lexicons: ["aws"],
  lint: {
    rules: {
      // Deliberate exposure: the benchmark scenario tests detection of the
      // open SSH security group, so the estate must really contain it.
      WAW019: "off",
      WAW049: "off",
    },
  },
} satisfies ChantConfig;
