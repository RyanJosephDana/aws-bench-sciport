# The ec2-multiregion estate, authored in Terraform. Same topology the CDK and
# chant arms deploy: us-east-1 carries the full estate (public + private subnet,
# open + unused security groups, IAM, a launch template, and four instances incl.
# one on the account default VPC); us-west-1 and us-west-2 each carry one public
# server. Stack names and SSM /exports match the scenario contract so the
# benchmark's ground truth resolves untouched.

module "primary" {
  source     = "./modules/primary_region"
  stack_name = "ec2-multiregion-EC2-ks84v1fh12-us-east-1"
  providers  = { aws = aws.use1 }
}

module "west1" {
  source     = "./modules/public_region"
  stack_name = "ec2-multiregion-EC2-ls9fuhb522-us-west-1"
  providers  = { aws = aws.usw1 }
}

module "west2" {
  source     = "./modules/public_region"
  stack_name = "ec2-multiregion-EC2-ls9fuhb522-us-west-2"
  providers  = { aws = aws.usw2 }
}

# ── QA roles stack (ec2-multiregion-QARoles-us-east-1) — no exports ──────────
data "aws_caller_identity" "use1" {
  provider = aws.use1
}

resource "aws_iam_policy" "s3_vectors_readonly" {
  provider    = aws.use1
  name        = "S3VectorsReadOnlyAccess-${data.aws_caller_identity.use1.account_id}-us-east-1"
  description = "Read-only access to S3 Vectors operations"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid    = "AllowS3VectorsReadOnlyAccess"
      Effect = "Allow"
      Action = [
        "s3vectors:ListVectors", "s3vectors:GetVectors", "s3vectors:GetIndex",
        "s3vectors:GetVectorBucket", "s3vectors:GetVectorBucketPolicy", "s3vectors:ListIndexes",
        "s3vectors:ListTagsForResource", "s3vectors:ListVectorBuckets", "s3vectors:QueryVectors",
      ]
      Resource = "*"
    }]
  })
}

locals {
  assume_by_account = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = "arn:aws:iam::${data.aws_caller_identity.use1.account_id}:root" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# NOTE: the CDK original also attaches arn:aws:iam::aws:policy/AmazonBedrockFullAccess
# to the readonly + judge roles. Floci does not pre-seed that AWS-managed policy,
# and Terraform's AttachRolePolicy (unlike CDK's CFN path) validates existence →
# 404. These QA roles are harness scaffolding the EC2 tasks never query, and the
# judge runs on OAuth (not Bedrock) in emulator mode, so the attachment is dropped
# until the Floci gap is fixed (floci-io/floci#2033, PR #2034 seeds it). Restore
# the arn once that lands in the emulator image.
resource "aws_iam_role" "qa_readonly" {
  provider           = aws.use1
  name               = "QALocalInvocationApplicationRole"
  assume_role_policy = local.assume_by_account
  managed_policy_arns = [
    "arn:aws:iam::aws:policy/ReadOnlyAccess",
    aws_iam_policy.s3_vectors_readonly.arn,
  ]
}

resource "aws_iam_role" "qa_admin" {
  provider            = aws.use1
  name                = "QALocalInvocationApplicationAdmin"
  assume_role_policy  = local.assume_by_account
  managed_policy_arns = ["arn:aws:iam::aws:policy/AdministratorAccess"]
}

resource "aws_iam_role" "qa_judge" {
  provider           = aws.use1
  name               = "LLMJudgeFullBedrockAccessRole"
  assume_role_policy = local.assume_by_account
  # AmazonBedrockFullAccess dropped — see note above (Floci gap, filed upstream).
  managed_policy_arns = [
    "arn:aws:iam::aws:policy/ReadOnlyAccess",
  ]
}
