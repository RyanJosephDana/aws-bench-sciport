#!/bin/bash
# Deploy the chant-built ec2-multiregion estate to Floci under the scenario's
# original stack names, so the benchmark's export contract resolves untouched.
set -euo pipefail
cd "$(dirname "$0")"

export AWS_ENDPOINT_URL=${AWS_ENDPOINT_URL:-http://localhost:4566}
export AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test
# A default region so the region-agnostic calls below (sts get-caller-identity,
# list-exports) resolve without an explicit --region on every invocation.
export AWS_DEFAULT_REGION=${AWS_DEFAULT_REGION:-us-east-1} AWS_REGION=${AWS_REGION:-us-east-1}

deploy() { # stack-name region template extra-params...
  local stack="$1" region="$2" template="$3"; shift 3
  aws cloudformation create-stack --stack-name "$stack" --region "$region" \
    --template-body "file://$template" --capabilities CAPABILITY_NAMED_IAM \
    "$@" >/dev/null
  aws cloudformation wait stack-create-complete --stack-name "$stack" --region "$region"
  echo "deployed $stack"
}

# Rebuild templates from source (idempotent).
for p in us-east-1 us-west-1 us-west-2 qa-roles; do
  (cd "$p" && npx chant build 2>/dev/null > template.json)
done

# The us-east-1 stack takes the live account's default VPC as parameters.
DEFAULT_VPC=$(aws ec2 describe-vpcs --region us-east-1 \
  --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)
read -r SUBNET1 SUBNET2 < <(aws ec2 describe-subnets --region us-east-1 \
  --filters Name=vpc-id,Values="$DEFAULT_VPC" \
  --query 'Subnets[0:2].SubnetId' --output text)

deploy ec2-multiregion-QARoles-us-east-1 us-east-1 qa-roles/template.json
deploy ec2-multiregion-EC2-ls9fuhb522-us-west-1 us-west-1 us-west-1/template.json
deploy ec2-multiregion-EC2-ls9fuhb522-us-west-2 us-west-2 us-west-2/template.json
deploy ec2-multiregion-EC2-ks84v1fh12-us-east-1 us-east-1 us-east-1/template.json \
  --parameters ParameterKey=defaultVpcId,ParameterValue="$DEFAULT_VPC" \
               ParameterKey=defaultSubnetId1,ParameterValue="$SUBNET1" \
               ParameterKey=defaultSubnetId2,ParameterValue="$SUBNET2"

# Bake the AMI the estate advertises (ami-<account>-us-east-1), from webServer.
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
WEB_INSTANCE=$(aws cloudformation list-exports --region us-east-1 --query \
  "Exports[?Name=='ec2-multiregion-EC2-ks84v1fh12-us-east-1-InstanceId'].Value" --output text)
aws ec2 create-image --region us-east-1 --instance-id "$WEB_INSTANCE" \
  --name "ami-${ACCOUNT}-us-east-1" \
  --description "AMI created from existing EC2 instance" >/dev/null
echo "baked ami-${ACCOUNT}-us-east-1 from $WEB_INSTANCE"

# Health: every instance the estate declares must be running.
for r in us-east-1 us-west-1 us-west-2; do
  aws ec2 describe-instances --region "$r" \
    --query 'Reservations[].Instances[].State.Name' --output text
done | tr '\t' '\n' | sort | uniq -c
