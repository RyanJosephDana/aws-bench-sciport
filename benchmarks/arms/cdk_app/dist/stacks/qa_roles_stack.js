"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.QARolesStack = void 0;
const aws_cdk_lib_1 = require("aws-cdk-lib");
const aws_iam_1 = require("aws-cdk-lib/aws-iam");
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
class QARolesStack extends aws_cdk_lib_1.Stack {
    constructor(scope, id, props) {
        super(scope, id, props);
        const accountId = this.account;
        // ── Custom policy: S3 Vectors read-only access ──
        const s3VectorsReadOnlyPolicy = new aws_iam_1.ManagedPolicy(this, 'S3VectorsReadOnlyAccess', {
            managedPolicyName: `S3VectorsReadOnlyAccess-${accountId}-${this.region}`,
            description: 'Read-only access to S3 Vectors operations',
            statements: [
                new aws_iam_1.PolicyStatement({
                    sid: 'AllowS3VectorsReadOnlyAccess',
                    effect: aws_iam_1.Effect.ALLOW,
                    actions: [
                        's3vectors:ListVectors',
                        's3vectors:GetVectors',
                        's3vectors:GetIndex',
                        's3vectors:GetVectorBucket',
                        's3vectors:GetVectorBucketPolicy',
                        's3vectors:ListIndexes',
                        's3vectors:ListTagsForResource',
                        's3vectors:ListVectorBuckets',
                        's3vectors:QueryVectors',
                    ],
                    resources: ['*'],
                }),
            ],
        });
        // ── QALocalInvocationApplicationRole (read-only for introspection) ──
        this.readonlyRole = new aws_iam_1.Role(this, 'QALocalInvocationApplicationRole', {
            roleName: 'QALocalInvocationApplicationRole',
            assumedBy: new aws_iam_1.AccountPrincipal(accountId),
            managedPolicies: [
                aws_iam_1.ManagedPolicy.fromAwsManagedPolicyName('ReadOnlyAccess'),
                aws_iam_1.ManagedPolicy.fromAwsManagedPolicyName('AmazonS3TablesReadOnlyAccess'),
                aws_iam_1.ManagedPolicy.fromAwsManagedPolicyName('AmazonRedshiftFullAccess'),
                aws_iam_1.ManagedPolicy.fromAwsManagedPolicyName('AmazonAthenaFullAccess'),
                aws_iam_1.ManagedPolicy.fromAwsManagedPolicyName('AmazonBedrockFullAccess'),
                s3VectorsReadOnlyPolicy,
            ],
        });
        // ── QALocalInvocationApplicationAdmin (admin for mutation) ──
        this.adminRole = new aws_iam_1.Role(this, 'QALocalInvocationApplicationAdmin', {
            roleName: 'QALocalInvocationApplicationAdmin',
            assumedBy: new aws_iam_1.AccountPrincipal(accountId),
            managedPolicies: [
                aws_iam_1.ManagedPolicy.fromAwsManagedPolicyName('AdministratorAccess'),
                aws_iam_1.ManagedPolicy.fromAwsManagedPolicyName('AmazonBedrockFullAccess'),
            ],
        });
        // ── LLMJudgeFullBedrockAccessRole (verifier) ──
        this.judgeRole = new aws_iam_1.Role(this, 'LLMJudgeFullBedrockAccessRole', {
            roleName: 'LLMJudgeFullBedrockAccessRole',
            assumedBy: new aws_iam_1.AccountPrincipal(accountId),
            managedPolicies: [
                aws_iam_1.ManagedPolicy.fromAwsManagedPolicyName('AmazonBedrockFullAccess'),
            ],
        });
    }
}
exports.QARolesStack = QARolesStack;
//# sourceMappingURL=data:application/json;base64,eyJ2ZXJzaW9uIjozLCJmaWxlIjoicWFfcm9sZXNfc3RhY2suanMiLCJzb3VyY2VSb290IjoiIiwic291cmNlcyI6WyIuLi8uLi9zdGFja3MvcWFfcm9sZXNfc3RhY2sudHMiXSwibmFtZXMiOltdLCJtYXBwaW5ncyI6Ijs7O0FBQUEsNkNBQWdEO0FBQ2hELGlEQUE0RztBQUc1Rzs7Ozs7Ozs7O0dBU0c7QUFDSCxNQUFhLFlBQWEsU0FBUSxtQkFBSztJQUtuQyxZQUFZLEtBQWdCLEVBQUUsRUFBVSxFQUFFLEtBQWtCO1FBQ3hELEtBQUssQ0FBQyxLQUFLLEVBQUUsRUFBRSxFQUFFLEtBQUssQ0FBQyxDQUFDO1FBRXhCLE1BQU0sU0FBUyxHQUFHLElBQUksQ0FBQyxPQUFPLENBQUM7UUFFL0IsbURBQW1EO1FBQ25ELE1BQU0sdUJBQXVCLEdBQUcsSUFBSSx1QkFBYSxDQUFDLElBQUksRUFBRSx5QkFBeUIsRUFBRTtZQUMvRSxpQkFBaUIsRUFBRSwyQkFBMkIsU0FBUyxJQUFJLElBQUksQ0FBQyxNQUFNLEVBQUU7WUFDeEUsV0FBVyxFQUFFLDJDQUEyQztZQUN4RCxVQUFVLEVBQUU7Z0JBQ1IsSUFBSSx5QkFBZSxDQUFDO29CQUNoQixHQUFHLEVBQUUsOEJBQThCO29CQUNuQyxNQUFNLEVBQUUsZ0JBQU0sQ0FBQyxLQUFLO29CQUNwQixPQUFPLEVBQUU7d0JBQ0wsdUJBQXVCO3dCQUN2QixzQkFBc0I7d0JBQ3RCLG9CQUFvQjt3QkFDcEIsMkJBQTJCO3dCQUMzQixpQ0FBaUM7d0JBQ2pDLHVCQUF1Qjt3QkFDdkIsK0JBQStCO3dCQUMvQiw2QkFBNkI7d0JBQzdCLHdCQUF3QjtxQkFDM0I7b0JBQ0QsU0FBUyxFQUFFLENBQUMsR0FBRyxDQUFDO2lCQUNuQixDQUFDO2FBQ0w7U0FDSixDQUFDLENBQUM7UUFFSCx1RUFBdUU7UUFDdkUsSUFBSSxDQUFDLFlBQVksR0FBRyxJQUFJLGNBQUksQ0FBQyxJQUFJLEVBQUUsa0NBQWtDLEVBQUU7WUFDbkUsUUFBUSxFQUFFLGtDQUFrQztZQUM1QyxTQUFTLEVBQUUsSUFBSSwwQkFBZ0IsQ0FBQyxTQUFTLENBQUM7WUFDMUMsZUFBZSxFQUFFO2dCQUNiLHVCQUFhLENBQUMsd0JBQXdCLENBQUMsZ0JBQWdCLENBQUM7Z0JBQ3hELHVCQUFhLENBQUMsd0JBQXdCLENBQUMsOEJBQThCLENBQUM7Z0JBQ3RFLHVCQUFhLENBQUMsd0JBQXdCLENBQUMsMEJBQTBCLENBQUM7Z0JBQ2xFLHVCQUFhLENBQUMsd0JBQXdCLENBQUMsd0JBQXdCLENBQUM7Z0JBQ2hFLHVCQUFhLENBQUMsd0JBQXdCLENBQUMseUJBQXlCLENBQUM7Z0JBQ2pFLHVCQUF1QjthQUMxQjtTQUNKLENBQUMsQ0FBQztRQUVILCtEQUErRDtRQUMvRCxJQUFJLENBQUMsU0FBUyxHQUFHLElBQUksY0FBSSxDQUFDLElBQUksRUFBRSxtQ0FBbUMsRUFBRTtZQUNqRSxRQUFRLEVBQUUsbUNBQW1DO1lBQzdDLFNBQVMsRUFBRSxJQUFJLDBCQUFnQixDQUFDLFNBQVMsQ0FBQztZQUMxQyxlQUFlLEVBQUU7Z0JBQ2IsdUJBQWEsQ0FBQyx3QkFBd0IsQ0FBQyxxQkFBcUIsQ0FBQztnQkFDN0QsdUJBQWEsQ0FBQyx3QkFBd0IsQ0FBQyx5QkFBeUIsQ0FBQzthQUNwRTtTQUNKLENBQUMsQ0FBQztRQUVILGlEQUFpRDtRQUNqRCxJQUFJLENBQUMsU0FBUyxHQUFHLElBQUksY0FBSSxDQUFDLElBQUksRUFBRSwrQkFBK0IsRUFBRTtZQUM3RCxRQUFRLEVBQUUsK0JBQStCO1lBQ3pDLFNBQVMsRUFBRSxJQUFJLDBCQUFnQixDQUFDLFNBQVMsQ0FBQztZQUMxQyxlQUFlLEVBQUU7Z0JBQ2IsdUJBQWEsQ0FBQyx3QkFBd0IsQ0FBQyx5QkFBeUIsQ0FBQzthQUNwRTtTQUNKLENBQUMsQ0FBQztJQUNQLENBQUM7Q0FDSjtBQW5FRCxvQ0FtRUMiLCJzb3VyY2VzQ29udGVudCI6WyJpbXBvcnQgeyBTdGFjaywgU3RhY2tQcm9wcyB9IGZyb20gJ2F3cy1jZGstbGliJztcbmltcG9ydCB7IEFjY291bnRQcmluY2lwYWwsIEVmZmVjdCwgSVJvbGUsIE1hbmFnZWRQb2xpY3ksIFBvbGljeVN0YXRlbWVudCwgUm9sZSB9IGZyb20gJ2F3cy1jZGstbGliL2F3cy1pYW0nO1xuaW1wb3J0IHsgQ29uc3RydWN0IH0gZnJvbSAnY29uc3RydWN0cyc7XG5cbi8qKlxuICogR2VuZXJpYyBRQSByb2xlcyBzdGFjayB0aGF0IGNyZWF0ZXMgdGhlIHRocmVlIHN0YW5kYXJkIHJvbGVzXG4gKiB1c2VkIGJ5IGF3cy1iZW5jaCBlbnZpcm9ubWVudHM6XG4gKlxuICogLSBRQUxvY2FsSW52b2NhdGlvbkFwcGxpY2F0aW9uUm9sZSAgKHJlYWQtb25seSBhZ2VudCByb2xlIGZvciBpbnRyb3NwZWN0aW9uIHRhc2tzKVxuICogLSBRQUxvY2FsSW52b2NhdGlvbkFwcGxpY2F0aW9uQWRtaW4gKGFkbWluIGFnZW50IHJvbGUgZm9yIG11dGF0aW9uIHRhc2tzKVxuICogLSBMTE1KdWRnZUZ1bGxCZWRyb2NrQWNjZXNzUm9sZSAgICAgKHZlcmlmaWVyIHJvbGUgZm9yIExMTS1iYXNlZCBqdWRnaW5nKVxuICpcbiAqIEFzc3VtZXMgb25lIGVudmlyb25tZW50IHBlciBhY2NvdW50IOKAlCByb2xlIG5hbWVzIGFyZSBub3QgZW52LXNjb3BlZC5cbiAqL1xuZXhwb3J0IGNsYXNzIFFBUm9sZXNTdGFjayBleHRlbmRzIFN0YWNrIHtcbiAgICBwdWJsaWMgcmVhZG9ubHkgcmVhZG9ubHlSb2xlOiBJUm9sZTtcbiAgICBwdWJsaWMgcmVhZG9ubHkgYWRtaW5Sb2xlOiBJUm9sZTtcbiAgICBwdWJsaWMgcmVhZG9ubHkganVkZ2VSb2xlOiBJUm9sZTtcblxuICAgIGNvbnN0cnVjdG9yKHNjb3BlOiBDb25zdHJ1Y3QsIGlkOiBzdHJpbmcsIHByb3BzPzogU3RhY2tQcm9wcykge1xuICAgICAgICBzdXBlcihzY29wZSwgaWQsIHByb3BzKTtcblxuICAgICAgICBjb25zdCBhY2NvdW50SWQgPSB0aGlzLmFjY291bnQ7XG5cbiAgICAgICAgLy8g4pSA4pSAIEN1c3RvbSBwb2xpY3k6IFMzIFZlY3RvcnMgcmVhZC1vbmx5IGFjY2VzcyDilIDilIBcbiAgICAgICAgY29uc3QgczNWZWN0b3JzUmVhZE9ubHlQb2xpY3kgPSBuZXcgTWFuYWdlZFBvbGljeSh0aGlzLCAnUzNWZWN0b3JzUmVhZE9ubHlBY2Nlc3MnLCB7XG4gICAgICAgICAgICBtYW5hZ2VkUG9saWN5TmFtZTogYFMzVmVjdG9yc1JlYWRPbmx5QWNjZXNzLSR7YWNjb3VudElkfS0ke3RoaXMucmVnaW9ufWAsXG4gICAgICAgICAgICBkZXNjcmlwdGlvbjogJ1JlYWQtb25seSBhY2Nlc3MgdG8gUzMgVmVjdG9ycyBvcGVyYXRpb25zJyxcbiAgICAgICAgICAgIHN0YXRlbWVudHM6IFtcbiAgICAgICAgICAgICAgICBuZXcgUG9saWN5U3RhdGVtZW50KHtcbiAgICAgICAgICAgICAgICAgICAgc2lkOiAnQWxsb3dTM1ZlY3RvcnNSZWFkT25seUFjY2VzcycsXG4gICAgICAgICAgICAgICAgICAgIGVmZmVjdDogRWZmZWN0LkFMTE9XLFxuICAgICAgICAgICAgICAgICAgICBhY3Rpb25zOiBbXG4gICAgICAgICAgICAgICAgICAgICAgICAnczN2ZWN0b3JzOkxpc3RWZWN0b3JzJyxcbiAgICAgICAgICAgICAgICAgICAgICAgICdzM3ZlY3RvcnM6R2V0VmVjdG9ycycsXG4gICAgICAgICAgICAgICAgICAgICAgICAnczN2ZWN0b3JzOkdldEluZGV4JyxcbiAgICAgICAgICAgICAgICAgICAgICAgICdzM3ZlY3RvcnM6R2V0VmVjdG9yQnVja2V0JyxcbiAgICAgICAgICAgICAgICAgICAgICAgICdzM3ZlY3RvcnM6R2V0VmVjdG9yQnVja2V0UG9saWN5JyxcbiAgICAgICAgICAgICAgICAgICAgICAgICdzM3ZlY3RvcnM6TGlzdEluZGV4ZXMnLFxuICAgICAgICAgICAgICAgICAgICAgICAgJ3MzdmVjdG9yczpMaXN0VGFnc0ZvclJlc291cmNlJyxcbiAgICAgICAgICAgICAgICAgICAgICAgICdzM3ZlY3RvcnM6TGlzdFZlY3RvckJ1Y2tldHMnLFxuICAgICAgICAgICAgICAgICAgICAgICAgJ3MzdmVjdG9yczpRdWVyeVZlY3RvcnMnLFxuICAgICAgICAgICAgICAgICAgICBdLFxuICAgICAgICAgICAgICAgICAgICByZXNvdXJjZXM6IFsnKiddLFxuICAgICAgICAgICAgICAgIH0pLFxuICAgICAgICAgICAgXSxcbiAgICAgICAgfSk7XG5cbiAgICAgICAgLy8g4pSA4pSAIFFBTG9jYWxJbnZvY2F0aW9uQXBwbGljYXRpb25Sb2xlIChyZWFkLW9ubHkgZm9yIGludHJvc3BlY3Rpb24pIOKUgOKUgFxuICAgICAgICB0aGlzLnJlYWRvbmx5Um9sZSA9IG5ldyBSb2xlKHRoaXMsICdRQUxvY2FsSW52b2NhdGlvbkFwcGxpY2F0aW9uUm9sZScsIHtcbiAgICAgICAgICAgIHJvbGVOYW1lOiAnUUFMb2NhbEludm9jYXRpb25BcHBsaWNhdGlvblJvbGUnLFxuICAgICAgICAgICAgYXNzdW1lZEJ5OiBuZXcgQWNjb3VudFByaW5jaXBhbChhY2NvdW50SWQpLFxuICAgICAgICAgICAgbWFuYWdlZFBvbGljaWVzOiBbXG4gICAgICAgICAgICAgICAgTWFuYWdlZFBvbGljeS5mcm9tQXdzTWFuYWdlZFBvbGljeU5hbWUoJ1JlYWRPbmx5QWNjZXNzJyksXG4gICAgICAgICAgICAgICAgTWFuYWdlZFBvbGljeS5mcm9tQXdzTWFuYWdlZFBvbGljeU5hbWUoJ0FtYXpvblMzVGFibGVzUmVhZE9ubHlBY2Nlc3MnKSxcbiAgICAgICAgICAgICAgICBNYW5hZ2VkUG9saWN5LmZyb21Bd3NNYW5hZ2VkUG9saWN5TmFtZSgnQW1hem9uUmVkc2hpZnRGdWxsQWNjZXNzJyksXG4gICAgICAgICAgICAgICAgTWFuYWdlZFBvbGljeS5mcm9tQXdzTWFuYWdlZFBvbGljeU5hbWUoJ0FtYXpvbkF0aGVuYUZ1bGxBY2Nlc3MnKSxcbiAgICAgICAgICAgICAgICBNYW5hZ2VkUG9saWN5LmZyb21Bd3NNYW5hZ2VkUG9saWN5TmFtZSgnQW1hem9uQmVkcm9ja0Z1bGxBY2Nlc3MnKSxcbiAgICAgICAgICAgICAgICBzM1ZlY3RvcnNSZWFkT25seVBvbGljeSxcbiAgICAgICAgICAgIF0sXG4gICAgICAgIH0pO1xuXG4gICAgICAgIC8vIOKUgOKUgCBRQUxvY2FsSW52b2NhdGlvbkFwcGxpY2F0aW9uQWRtaW4gKGFkbWluIGZvciBtdXRhdGlvbikg4pSA4pSAXG4gICAgICAgIHRoaXMuYWRtaW5Sb2xlID0gbmV3IFJvbGUodGhpcywgJ1FBTG9jYWxJbnZvY2F0aW9uQXBwbGljYXRpb25BZG1pbicsIHtcbiAgICAgICAgICAgIHJvbGVOYW1lOiAnUUFMb2NhbEludm9jYXRpb25BcHBsaWNhdGlvbkFkbWluJyxcbiAgICAgICAgICAgIGFzc3VtZWRCeTogbmV3IEFjY291bnRQcmluY2lwYWwoYWNjb3VudElkKSxcbiAgICAgICAgICAgIG1hbmFnZWRQb2xpY2llczogW1xuICAgICAgICAgICAgICAgIE1hbmFnZWRQb2xpY3kuZnJvbUF3c01hbmFnZWRQb2xpY3lOYW1lKCdBZG1pbmlzdHJhdG9yQWNjZXNzJyksXG4gICAgICAgICAgICAgICAgTWFuYWdlZFBvbGljeS5mcm9tQXdzTWFuYWdlZFBvbGljeU5hbWUoJ0FtYXpvbkJlZHJvY2tGdWxsQWNjZXNzJyksXG4gICAgICAgICAgICBdLFxuICAgICAgICB9KTtcblxuICAgICAgICAvLyDilIDilIAgTExNSnVkZ2VGdWxsQmVkcm9ja0FjY2Vzc1JvbGUgKHZlcmlmaWVyKSDilIDilIBcbiAgICAgICAgdGhpcy5qdWRnZVJvbGUgPSBuZXcgUm9sZSh0aGlzLCAnTExNSnVkZ2VGdWxsQmVkcm9ja0FjY2Vzc1JvbGUnLCB7XG4gICAgICAgICAgICByb2xlTmFtZTogJ0xMTUp1ZGdlRnVsbEJlZHJvY2tBY2Nlc3NSb2xlJyxcbiAgICAgICAgICAgIGFzc3VtZWRCeTogbmV3IEFjY291bnRQcmluY2lwYWwoYWNjb3VudElkKSxcbiAgICAgICAgICAgIG1hbmFnZWRQb2xpY2llczogW1xuICAgICAgICAgICAgICAgIE1hbmFnZWRQb2xpY3kuZnJvbUF3c01hbmFnZWRQb2xpY3lOYW1lKCdBbWF6b25CZWRyb2NrRnVsbEFjY2VzcycpLFxuICAgICAgICAgICAgXSxcbiAgICAgICAgfSk7XG4gICAgfVxufVxuIl19