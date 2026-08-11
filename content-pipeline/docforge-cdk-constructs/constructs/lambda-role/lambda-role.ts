import * as cdk from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';

/**
 * A single inline policy statement definition driven by config.
 */
export interface PolicyStatementConfig {
  actions: string[];
  resources: string[];
  effect?: 'Allow' | 'Deny';
}

/**
 * Props for the LambdaRoleConstruct.
 *
 * Designed to be driven entirely from YAML config:
 *   role:
 *     name: "shared-d-use1-my_lambda-role"
 *     kms_decrypt_key_arn: "arn:aws:kms:..."   # optional
 *     policies:
 *       - actions: [s3:GetObject]
 *         resources: ["arn:aws:s3:::my-bucket/*"]
 */
export interface LambdaRoleConfig {
  /** Explicit role name. If omitted CDK auto-generates one. */
  roleName?: string;
  /** Optional description for the IAM role. */
  description?: string;
  /** If provided, adds a kms:Decrypt policy scoped to this key ARN with LambdaFunctionName condition. */
  kmsDecryptKeyArn?: string;
  /** Lambda function name — used for KMS condition and log group scoping. */
  functionName?: string;
  /** Additional inline policy statements beyond the basics. */
  policies?: PolicyStatementConfig[];
  /** Tags to apply. */
  tags?: Record<string, string>;
}

export class LambdaRoleConstruct extends Construct {
  public readonly role: iam.Role;

  constructor(scope: Construct, id: string, config: LambdaRoleConfig) {
    super(scope, id);

    this.role = new iam.Role(this, 'Role', {
      ...(config.roleName ? { roleName: config.roleName } : {}),
      ...(config.description ? { description: config.description } : {}),
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole'),
      ],
    });

    // KMS decrypt for environment variable encryption
    if (config.kmsDecryptKeyArn) {
      // If the value is a KMS alias (not a full ARN), convert to proper ARN format
      let kmsResource = config.kmsDecryptKeyArn;
      if (kmsResource.startsWith('alias/')) {
        kmsResource = `arn:aws:kms:${cdk.Stack.of(this).region}:${cdk.Stack.of(this).account}:${kmsResource}`;
      }
      const kmsStatement = new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['kms:Decrypt'],
        resources: [kmsResource],
      });
      // Scope to this lambda's function name if provided
      if (config.functionName) {
        kmsStatement.addCondition('StringLike', {
          'kms:EncryptionContext:LambdaFunctionName': config.functionName,
        });
      }
      this.role.addToPolicy(kmsStatement);
    }

    // Additional inline policies from config
    if (config.policies) {
      for (const [, p] of config.policies.entries()) {
        this.role.addToPolicy(
          new iam.PolicyStatement({
            effect: p.effect === 'Deny' ? iam.Effect.DENY : iam.Effect.ALLOW,
            actions: p.actions,
            resources: p.resources,
          })
        );
      }
    }

    // Tags
    if (config.tags) {
      Object.entries(config.tags).forEach(([k, v]) => {
        cdk.Tags.of(this).add(k, v);
      });
    }
  }
}
