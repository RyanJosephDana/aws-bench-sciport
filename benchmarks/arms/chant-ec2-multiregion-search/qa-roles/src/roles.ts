// The scenario's QA roles stack — deployed by chant under the stack name
// "ec2-multiregion-QARoles-us-east-1". Three account-scoped roles the
// aws-bench environment expects (no exports):
//   QALocalInvocationApplicationRole   read-only agent role (introspection)
//   QALocalInvocationApplicationAdmin  admin agent role (mutation)
//   LLMJudgeFullBedrockAccessRole      verifier role (LLM judging)

import { Role, ManagedPolicy, Sub, AWS, stackOutput } from "@intentius/chant-lexicon-aws";

const assumeByAccount = {
  Version: "2012-10-17",
  Statement: [
    {
      Effect: "Allow",
      Principal: { AWS: Sub`arn:aws:iam::${AWS.AccountId}:root` },
      Action: "sts:AssumeRole",
    },
  ],
};

export const s3VectorsReadOnly = new ManagedPolicy({
  ManagedPolicyName: Sub`S3VectorsReadOnlyAccess-${AWS.AccountId}-${AWS.Region}`,
  Description: "Read-only access to S3 Vectors operations",
  PolicyDocument: {
    Version: "2012-10-17",
    Statement: [
      {
        Sid: "AllowS3VectorsReadOnlyAccess",
        Effect: "Allow",
        Action: [
          "s3vectors:ListVectors",
          "s3vectors:GetVectors",
          "s3vectors:GetIndex",
          "s3vectors:GetVectorBucket",
          "s3vectors:GetVectorBucketPolicy",
          "s3vectors:ListIndexes",
          "s3vectors:ListTagsForResource",
          "s3vectors:ListVectorBuckets",
          "s3vectors:QueryVectors",
        ],
        Resource: "*",
      },
    ],
  },
});

export const readonlyRole = new Role({
  RoleName: "QALocalInvocationApplicationRole",
  AssumeRolePolicyDocument: assumeByAccount,
  ManagedPolicyArns: [
    "arn:aws:iam::aws:policy/ReadOnlyAccess",
    "arn:aws:iam::aws:policy/AmazonBedrockFullAccess",
    s3VectorsReadOnly.PolicyArn,
  ],
});

export const adminRole = new Role({
  RoleName: "QALocalInvocationApplicationAdmin",
  AssumeRolePolicyDocument: assumeByAccount,
  ManagedPolicyArns: ["arn:aws:iam::aws:policy/AdministratorAccess"],
});

export const judgeRole = new Role({
  RoleName: "LLMJudgeFullBedrockAccessRole",
  AssumeRolePolicyDocument: assumeByAccount,
  ManagedPolicyArns: [
    "arn:aws:iam::aws:policy/AmazonBedrockFullAccess",
    "arn:aws:iam::aws:policy/ReadOnlyAccess",
  ],
});
