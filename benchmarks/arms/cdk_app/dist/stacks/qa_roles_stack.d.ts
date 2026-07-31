import { Stack, StackProps } from 'aws-cdk-lib';
import { IRole } from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';
/**
 * Generic QA roles stack that creates the three standard roles
 * used by aws-bench environments:
 *
 * - QALocalInvocationApplicationRole  (read-only agent role for introspection tasks)
 * - QALocalInvocationApplicationAdmin (admin agent role for mutation tasks)
 * - LLMJudgeFullBedrockAccessRole     (verifier role for LLM-based judging)
 *
 * Assumes one environment per account — role names are not env-scoped.
 */
export declare class QARolesStack extends Stack {
    readonly readonlyRole: IRole;
    readonly adminRole: IRole;
    readonly judgeRole: IRole;
    constructor(scope: Construct, id: string, props?: StackProps);
}
