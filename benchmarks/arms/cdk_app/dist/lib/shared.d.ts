import * as cdk from 'aws-cdk-lib';
export interface EnvironmentProps {
    readonly account: string;
}
export declare class StackUtils {
    static exportStack(stack: cdk.Stack, name: string, value: string, description?: string): cdk.CfnOutput;
}
