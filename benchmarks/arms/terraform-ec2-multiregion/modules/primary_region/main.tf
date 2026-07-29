terraform {
  required_providers {
    aws = {
      source                = "hashicorp/aws"
      configuration_aliases = [aws]
    }
  }
}

variable "stack_name" {
  type        = string
  description = "The scenario stack name this region deploys under (prefixes its exports)."
}

data "aws_caller_identity" "current" {}

# The account default VPC (Floci auto-creates one per region). The default-VPC
# instance lands here — internet-facing (default VPC subnets route to an IGW)
# but carrying no port-22 rule, so public yet not SSH-reachable.
data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

locals {
  account         = data.aws_caller_identity.current.account_id
  default_subnets = data.aws_subnets.default.ids
}

# ── Custom VPC: one public, one isolated private subnet ─────────────────────
resource "aws_vpc" "main" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = "ResourcesVpc" }
}

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.0.0.0/24"
  map_public_ip_on_launch = true
  tags                    = { Name = "Public" }
}

resource "aws_subnet" "private" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.0.1.0/24"
  map_public_ip_on_launch = false
  tags                    = { Name = "Private" }
}

resource "aws_internet_gateway" "igw" {
  vpc_id = aws_vpc.main.id
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
}

resource "aws_route" "default" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.igw.id
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

# ── Security groups ─────────────────────────────────────────────────────────
# SSH/HTTP/HTTPS open to the world — the scenario's deliberate exposure.
resource "aws_security_group" "web" {
  name        = "web-${var.stack_name}"
  description = "Allow specific inbound traffic"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "Allow SSH access"
    protocol    = "tcp"
    from_port   = 22
    to_port     = 22
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    description = "Allow HTTP access"
    protocol    = "tcp"
    from_port   = 80
    to_port     = 80
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    description = "Allow HTTPS access"
    protocol    = "tcp"
    from_port   = 443
    to_port     = 443
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# Attached to nothing, on purpose.
resource "aws_security_group" "unused" {
  name        = "unused-${var.stack_name}"
  description = "Unused security group"
  vpc_id      = aws_vpc.main.id
}

# ── IAM ─────────────────────────────────────────────────────────────────────
resource "aws_iam_role" "instance" {
  name = "role-${local.account}-us-east-1"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  managed_policy_arns = ["arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"]

  inline_policy {
    name = "policy-${local.account}-us-east-1"
    policy = jsonencode({
      Version = "2012-10-17"
      Statement = [{
        Effect   = "Deny"
        Action   = ["ec2:ModifyInstanceAttribute", "ec2:ModifyInstanceMetadataOptions"]
        Resource = "*"
      }]
    })
  }
}

resource "aws_iam_instance_profile" "instance" {
  name = "instanceprofile-${local.account}-us-east-1"
  role = aws_iam_role.instance.name
}

# ── Instances ───────────────────────────────────────────────────────────────
resource "aws_instance" "web_server" {
  ami                    = "ami-0f3f13f145e66a0a3"
  instance_type          = "t3.micro"
  subnet_id              = aws_subnet.public.id
  vpc_security_group_ids = [aws_security_group.web.id]
  iam_instance_profile   = aws_iam_instance_profile.instance.name
  tags                   = { Name = "WebServerInstance" }
}

resource "aws_launch_template" "this" {
  name                   = "lt-${local.account}-us-east-1"
  image_id               = "ami-0f3f13f145e66a0a3"
  instance_type          = "t3.micro"
  vpc_security_group_ids = [aws_security_group.web.id]

  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size = 8
      volume_type = "gp3"
    }
  }
  metadata_options {
    http_tokens = "required"
  }
}

# Reachable ONLY through the launch template's security group — the hop a raw
# CLI sweep misses.
resource "aws_instance" "launch_template_server" {
  subnet_id = aws_subnet.public.id
  launch_template {
    id      = aws_launch_template.this.id
    version = "1"
  }
  tags = { Name = "LaunchTemplateInstance" }
}

# Isolated private subnet — must not appear in public-reachability answers.
resource "aws_instance" "private_server" {
  ami           = "ami-0f3f13f145e66a0a3"
  instance_type = "t3.micro"
  subnet_id     = aws_subnet.private.id
  tags          = { Name = "PrivateInstance" }
}

# Account default VPC — internet-facing subnet, but no port-22 rule.
resource "aws_instance" "default_vpc_server" {
  ami           = "ami-0f3f13f145e66a0a3"
  instance_type = "t3.micro"
  subnet_id     = local.default_subnets[0]
  tags          = { Name = "MyEC2Instance" }
}

# ── Export contract → SSM /exports (27 exports, names identical to CDK's) ────
locals {
  exports = {
    "${var.stack_name}-PrivateInstanceId"                            = aws_instance.private_server.id
    "${var.stack_name}-PrivateInstancePrivateIp"                     = aws_instance.private_server.private_ip
    "${var.stack_name}-RoleName"                                     = aws_iam_role.instance.name
    "${var.stack_name}-VpcId"                                        = aws_vpc.main.id
    "${var.stack_name}-RestrictedActionModifyInstanceAttribute"      = "ec2:ModifyInstanceAttribute"
    "${var.stack_name}-RestrictedActionModifyInstanceMetadataOptions" = "ec2:ModifyInstanceMetadataOptions"
    "${var.stack_name}-SecurityGroupId"                              = aws_security_group.web.id
    "${var.stack_name}-UnusedSecurityGroupId"                        = aws_security_group.unused.id
    "${var.stack_name}-PublicSubnetId"                               = aws_subnet.public.id
    "${var.stack_name}-InstanceProfileName"                          = aws_iam_instance_profile.instance.name
    "${var.stack_name}-AMIName"                                      = "ami-${local.account}-us-east-1"
    "${var.stack_name}-InstanceId"                                   = aws_instance.web_server.id
    "${var.stack_name}-LaunchTemplateInstanceId"                     = aws_instance.launch_template_server.id
    "${var.stack_name}-NUMEC2Running"                                = "4"
    "${var.stack_name}-NUMSubnets"                                   = "1"
    "${var.stack_name}-OpenSSHPort"                                  = "22"
    "${var.stack_name}-OpenHTTPPort"                                 = "80"
    "${var.stack_name}-OpenHTTPsPort"                                = "443"
    "${var.stack_name}-StackName"                                    = var.stack_name
    "${var.stack_name}-StackId"                                      = var.stack_name
    "${var.stack_name}-PrivateIPOfInstanceWithoutLaunchTemplate"     = aws_instance.web_server.private_ip
    "${var.stack_name}-PrivateIPOfInstanceWithLaunchTemplate"        = aws_instance.launch_template_server.private_ip
    "${var.stack_name}-DefaultVpcId"                                 = data.aws_vpc.default.id
    "${var.stack_name}-DefaultPublicSubnetId1"                       = local.default_subnets[0]
    "${var.stack_name}-DefaultPublicSubnetId2"                       = try(local.default_subnets[1], local.default_subnets[0])
    "${var.stack_name}-DefaultVPCInstanceId"                         = aws_instance.default_vpc_server.id
    "${var.stack_name}-PrivateIPOfInstanceWithDefaultVpc"            = aws_instance.default_vpc_server.private_ip
  }
}

resource "aws_ssm_parameter" "exports" {
  for_each = local.exports
  name     = "/exports/${each.key}"
  type     = "String"
  value    = each.value
}
