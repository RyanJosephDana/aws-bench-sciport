// The ec2-multiregion estate, authored in Pulumi (TypeScript). Same topology the
// CDK, chant, and Terraform arms deploy: us-east-1 carries the full estate (public
// + private subnet, open + unused security groups, IAM, a launch template, four
// instances incl. one on the account default VPC); us-west-1 and us-west-2 each
// carry one public server. Stack names and SSM /exports match the scenario contract
// so the benchmark's ground truth resolves untouched.
import * as pulumi from "@pulumi/pulumi";
import * as aws from "@pulumi/aws";

const AMI = "ami-0f3f13f145e66a0a3";
// The emulator's host port is not always 4566 — another Floci on the machine
// takes it first — so run-arm.sh exports AWS_ENDPOINT_URL and this follows it.
// Every other arm picks the endpoint up from the environment already; this one
// had the number compiled in, so it would have deployed against whatever was
// on 4566 rather than against the benchmark's emulator.
const FLOCI = process.env.AWS_ENDPOINT_URL || "http://localhost:4566";
const endpoints = [{ ec2: FLOCI, sts: FLOCI, iam: FLOCI, ssm: FLOCI }];

function fprovider(alias: string, region: aws.Region): aws.Provider {
  return new aws.Provider(alias, {
    region, accessKey: "test", secretKey: "test",
    skipCredentialsValidation: true, skipMetadataApiCheck: true, skipRegionValidation: true,
    endpoints,
  });
}

const use1 = fprovider("use1", "us-east-1");
const usw1 = fprovider("usw1", "us-west-1");
const usw2 = fprovider("usw2", "us-west-2");

// SSM /exports — the benchmark's export collector reads these the same as CFN
// exports, keyed by the last path segment.
function exp(name: string, value: pulumi.Input<string>, provider: aws.Provider): void {
  new aws.ssm.Parameter(`exp-${name}`, { name: `/exports/${name}`, type: "String", value }, { provider });
}

// ── A public-only region (us-west-1, us-west-2): VPC + public subnet + IGW + one
// server with no explicit SG (default SG) — internet-facing but not SSH-reachable.
function publicRegion(alias: string, provider: aws.Provider, stack: string): void {
  const vpc = new aws.ec2.Vpc(`${alias}-vpc`, {
    cidrBlock: "10.0.0.0/16", enableDnsSupport: true, enableDnsHostnames: true,
    tags: { Name: "ResourcesVpc" },
  }, { provider });
  const subnet = new aws.ec2.Subnet(`${alias}-public`, {
    vpcId: vpc.id, cidrBlock: "10.0.0.0/24", mapPublicIpOnLaunch: true, tags: { Name: "Public" },
  }, { provider });
  const igw = new aws.ec2.InternetGateway(`${alias}-igw`, { vpcId: vpc.id }, { provider });
  const rt = new aws.ec2.RouteTable(`${alias}-rt`, { vpcId: vpc.id }, { provider });
  new aws.ec2.Route(`${alias}-route`, { routeTableId: rt.id, destinationCidrBlock: "0.0.0.0/0", gatewayId: igw.id }, { provider });
  new aws.ec2.RouteTableAssociation(`${alias}-assoc`, { subnetId: subnet.id, routeTableId: rt.id }, { provider });
  const server = new aws.ec2.Instance(`${alias}-server`, {
    ami: AMI, instanceType: "t3.micro", subnetId: subnet.id, tags: { Name: "WebServerInstance" },
  }, { provider });
  exp(`${stack}-VpcId`, vpc.id, provider);
  exp(`${stack}-InstanceId`, server.id, provider);
  exp(`${stack}-PublicSubnetId`, subnet.id, provider);
  exp(`${stack}-NUMEC2Running`, "1", provider);
  exp(`${stack}-PrivateIPOfInstance`, server.privateIp, provider);
}

publicRegion("usw1", usw1, "ec2-multiregion-EC2-ls9fuhb522-us-west-1");
publicRegion("usw2", usw2, "ec2-multiregion-EC2-ls9fuhb522-us-west-2");

// ── us-east-1 (primary) ──────────────────────────────────────────────────────
const STACK = "ec2-multiregion-EC2-ks84v1fh12-us-east-1";
const account = aws.getCallerIdentityOutput({}, { provider: use1 }).accountId;
const defVpc = aws.ec2.getVpcOutput({ default: true }, { provider: use1 });
const defSubnets = aws.ec2.getSubnetsOutput({ filters: [{ name: "vpc-id", values: [defVpc.id] }] }, { provider: use1 });

const vpc = new aws.ec2.Vpc("vpc", {
  cidrBlock: "10.0.0.0/16", enableDnsSupport: true, enableDnsHostnames: true, tags: { Name: "ResourcesVpc" },
}, { provider: use1 });
const publicSubnet = new aws.ec2.Subnet("public", {
  vpcId: vpc.id, cidrBlock: "10.0.0.0/24", mapPublicIpOnLaunch: true, tags: { Name: "Public" },
}, { provider: use1 });
const privateSubnet = new aws.ec2.Subnet("private", {
  vpcId: vpc.id, cidrBlock: "10.0.1.0/24", mapPublicIpOnLaunch: false, tags: { Name: "Private" },
}, { provider: use1 });
const igw = new aws.ec2.InternetGateway("igw", { vpcId: vpc.id }, { provider: use1 });
const rt = new aws.ec2.RouteTable("rt", { vpcId: vpc.id }, { provider: use1 });
new aws.ec2.Route("route", { routeTableId: rt.id, destinationCidrBlock: "0.0.0.0/0", gatewayId: igw.id }, { provider: use1 });
new aws.ec2.RouteTableAssociation("assoc", { subnetId: publicSubnet.id, routeTableId: rt.id }, { provider: use1 });

const webSg = new aws.ec2.SecurityGroup("web", {
  name: `web-${STACK}`, description: "Allow specific inbound traffic", vpcId: vpc.id,
  ingress: [
    { description: "Allow SSH access", protocol: "tcp", fromPort: 22, toPort: 22, cidrBlocks: ["0.0.0.0/0"] },
    { description: "Allow HTTP access", protocol: "tcp", fromPort: 80, toPort: 80, cidrBlocks: ["0.0.0.0/0"] },
    { description: "Allow HTTPS access", protocol: "tcp", fromPort: 443, toPort: 443, cidrBlocks: ["0.0.0.0/0"] },
  ],
}, { provider: use1 });
const unusedSg = new aws.ec2.SecurityGroup("unused", {
  name: `unused-${STACK}`, description: "Unused security group", vpcId: vpc.id,
}, { provider: use1 });

const role = new aws.iam.Role("instance", {
  name: pulumi.interpolate`role-${account}-us-east-1`,
  assumeRolePolicy: JSON.stringify({ Version: "2012-10-17", Statement: [{ Effect: "Allow", Principal: { Service: "ec2.amazonaws.com" }, Action: "sts:AssumeRole" }] }),
  managedPolicyArns: ["arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"],
  inlinePolicies: [{ name: pulumi.interpolate`policy-${account}-us-east-1`, policy: JSON.stringify({ Version: "2012-10-17", Statement: [{ Effect: "Deny", Action: ["ec2:ModifyInstanceAttribute", "ec2:ModifyInstanceMetadataOptions"], Resource: "*" }] }) }],
}, { provider: use1 });
const profile = new aws.iam.InstanceProfile("profile", { name: pulumi.interpolate`instanceprofile-${account}-us-east-1`, role: role.name }, { provider: use1 });

const webServer = new aws.ec2.Instance("webServer", {
  ami: AMI, instanceType: "t3.micro", subnetId: publicSubnet.id,
  vpcSecurityGroupIds: [webSg.id], iamInstanceProfile: profile.name, tags: { Name: "WebServerInstance" },
}, { provider: use1 });

const lt = new aws.ec2.LaunchTemplate("lt", {
  name: pulumi.interpolate`lt-${account}-us-east-1`, imageId: AMI, instanceType: "t3.micro",
  vpcSecurityGroupIds: [webSg.id],
  blockDeviceMappings: [{ deviceName: "/dev/xvda", ebs: { volumeSize: 8, volumeType: "gp3" } }],
  metadataOptions: { httpTokens: "required" },
}, { provider: use1 });
// Reachable ONLY through the launch template's security group — the hop a raw CLI sweep misses.
const ltServer = new aws.ec2.Instance("ltServer", {
  subnetId: publicSubnet.id, launchTemplate: { id: lt.id, version: "1" }, tags: { Name: "LaunchTemplateInstance" },
}, { provider: use1 });
const privateServer = new aws.ec2.Instance("privateServer", {
  ami: AMI, instanceType: "t3.micro", subnetId: privateSubnet.id, tags: { Name: "PrivateInstance" },
}, { provider: use1 });
const defaultVpcServer = new aws.ec2.Instance("defaultVpcServer", {
  ami: AMI, instanceType: "t3.micro", subnetId: defSubnets.ids[0], tags: { Name: "MyEC2Instance" },
}, { provider: use1 });

// ── us-east-1 export contract (27) ──────────────────────────────────────────
exp(`${STACK}-PrivateInstanceId`, privateServer.id, use1);
exp(`${STACK}-PrivateInstancePrivateIp`, privateServer.privateIp, use1);
exp(`${STACK}-RoleName`, role.name, use1);
exp(`${STACK}-VpcId`, vpc.id, use1);
exp(`${STACK}-RestrictedActionModifyInstanceAttribute`, "ec2:ModifyInstanceAttribute", use1);
exp(`${STACK}-RestrictedActionModifyInstanceMetadataOptions`, "ec2:ModifyInstanceMetadataOptions", use1);
exp(`${STACK}-SecurityGroupId`, webSg.id, use1);
exp(`${STACK}-UnusedSecurityGroupId`, unusedSg.id, use1);
exp(`${STACK}-PublicSubnetId`, publicSubnet.id, use1);
exp(`${STACK}-InstanceProfileName`, profile.name, use1);
exp(`${STACK}-AMIName`, pulumi.interpolate`ami-${account}-us-east-1`, use1);
exp(`${STACK}-InstanceId`, webServer.id, use1);
exp(`${STACK}-LaunchTemplateInstanceId`, ltServer.id, use1);
exp(`${STACK}-NUMEC2Running`, "4", use1);
exp(`${STACK}-NUMSubnets`, "1", use1);
exp(`${STACK}-OpenSSHPort`, "22", use1);
exp(`${STACK}-OpenHTTPPort`, "80", use1);
exp(`${STACK}-OpenHTTPsPort`, "443", use1);
exp(`${STACK}-StackName`, STACK, use1);
exp(`${STACK}-StackId`, STACK, use1);
exp(`${STACK}-PrivateIPOfInstanceWithoutLaunchTemplate`, webServer.privateIp, use1);
exp(`${STACK}-PrivateIPOfInstanceWithLaunchTemplate`, ltServer.privateIp, use1);
exp(`${STACK}-DefaultVpcId`, defVpc.id, use1);
exp(`${STACK}-DefaultPublicSubnetId1`, defSubnets.ids[0], use1);
exp(`${STACK}-DefaultPublicSubnetId2`, defSubnets.ids.apply(ids => ids.length > 1 ? ids[1] : ids[0]), use1);
exp(`${STACK}-DefaultVPCInstanceId`, defaultVpcServer.id, use1);
exp(`${STACK}-PrivateIPOfInstanceWithDefaultVpc`, defaultVpcServer.privateIp, use1);

// ── QA roles (ec2-multiregion-QARoles-us-east-1) — no exports ────────────────
// NOTE: the CDK original also attaches arn:aws:iam::aws:policy/AmazonBedrockFullAccess
// to the readonly + judge roles. Floci does not pre-seed that managed policy and the
// Pulumi AWS provider (like Terraform) calls AttachRolePolicy, which 404s. These QA
// roles are harness scaffolding the EC2 tasks never query, and the judge runs on OAuth
// in emulator mode, so the attachment is dropped until Floci seeds it
// (floci-io/floci#2033, PR #2034 seeds it). Restore once that lands.
const assumeByAccount = pulumi.interpolate`{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"AWS":"arn:aws:iam::${account}:root"},"Action":"sts:AssumeRole"}]}`;
const s3Vectors = new aws.iam.Policy("s3vectors", {
  name: pulumi.interpolate`S3VectorsReadOnlyAccess-${account}-us-east-1`,
  description: "Read-only access to S3 Vectors operations",
  policy: JSON.stringify({ Version: "2012-10-17", Statement: [{ Sid: "AllowS3VectorsReadOnlyAccess", Effect: "Allow", Action: ["s3vectors:ListVectors", "s3vectors:GetVectors", "s3vectors:GetIndex", "s3vectors:GetVectorBucket", "s3vectors:GetVectorBucketPolicy", "s3vectors:ListIndexes", "s3vectors:ListTagsForResource", "s3vectors:ListVectorBuckets", "s3vectors:QueryVectors"], Resource: "*" }] }),
}, { provider: use1 });
new aws.iam.Role("qaReadonly", { name: "QALocalInvocationApplicationRole", assumeRolePolicy: assumeByAccount, managedPolicyArns: ["arn:aws:iam::aws:policy/ReadOnlyAccess", s3Vectors.arn] }, { provider: use1 });
new aws.iam.Role("qaAdmin", { name: "QALocalInvocationApplicationAdmin", assumeRolePolicy: assumeByAccount, managedPolicyArns: ["arn:aws:iam::aws:policy/AdministratorAccess"] }, { provider: use1 });
new aws.iam.Role("qaJudge", { name: "LLMJudgeFullBedrockAccessRole", assumeRolePolicy: assumeByAccount, managedPolicyArns: ["arn:aws:iam::aws:policy/ReadOnlyAccess"] }, { provider: use1 });
