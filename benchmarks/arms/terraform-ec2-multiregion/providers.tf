# Three regional providers, all pointed at the local Floci emulator. The estate
# spans us-east-1 (primary, full topology), us-west-1 and us-west-2 (one public
# server each). Credentials are throwaway; Floci does not verify signatures.
# Overridable so the estate can be deployed from inside a container, where the
# emulator is reachable as host.docker.internal rather than localhost. The
# default is unchanged, so reading or running this from the host behaves exactly
# as before.
variable "floci_endpoint" {
  type        = string
  description = "Base URL of the Floci emulator."
  default     = "http://localhost:4566"
}

locals {
  floci_endpoint = var.floci_endpoint
}

provider "aws" {
  alias                       = "use1"
  region                      = "us-east-1"
  access_key                  = "test"
  secret_key                  = "test"
  skip_credentials_validation = true
  skip_metadata_api_check     = true
  skip_region_validation      = true

  endpoints {
    ec2 = local.floci_endpoint
    sts = local.floci_endpoint
    iam = local.floci_endpoint
    ssm = local.floci_endpoint
  }
}

provider "aws" {
  alias                       = "usw1"
  region                      = "us-west-1"
  access_key                  = "test"
  secret_key                  = "test"
  skip_credentials_validation = true
  skip_metadata_api_check     = true
  skip_region_validation      = true

  endpoints {
    ec2 = local.floci_endpoint
    sts = local.floci_endpoint
    iam = local.floci_endpoint
    ssm = local.floci_endpoint
  }
}

provider "aws" {
  alias                       = "usw2"
  region                      = "us-west-2"
  access_key                  = "test"
  secret_key                  = "test"
  skip_credentials_validation = true
  skip_metadata_api_check     = true
  skip_region_validation      = true

  endpoints {
    ec2 = local.floci_endpoint
    sts = local.floci_endpoint
    iam = local.floci_endpoint
    ssm = local.floci_endpoint
  }
}
