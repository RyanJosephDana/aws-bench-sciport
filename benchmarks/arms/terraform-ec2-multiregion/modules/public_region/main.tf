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

# One public subnet, an internet gateway, and a single web server. The server
# carries no explicit security group (it lands on the VPC default SG), so it is
# in a public subnet but NOT reachable on port 22 — internet-facing yet not
# SSH-reachable.
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

resource "aws_instance" "server" {
  ami           = "ami-0f3f13f145e66a0a3"
  instance_type = "t3.micro"
  subnet_id     = aws_subnet.public.id
  tags          = { Name = "WebServerInstance" }
}

# Export contract → SSM /exports (the benchmark's export collector reads these
# the same as CloudFormation exports, keyed by the last path segment).
locals {
  exports = {
    "${var.stack_name}-VpcId"             = aws_vpc.main.id
    "${var.stack_name}-InstanceId"        = aws_instance.server.id
    "${var.stack_name}-PublicSubnetId"    = aws_subnet.public.id
    "${var.stack_name}-NUMEC2Running"     = "1"
    "${var.stack_name}-PrivateIPOfInstance" = aws_instance.server.private_ip
  }
}

resource "aws_ssm_parameter" "exports" {
  for_each = local.exports
  name     = "/exports/${each.key}"
  type     = "String"
  value    = each.value
}
