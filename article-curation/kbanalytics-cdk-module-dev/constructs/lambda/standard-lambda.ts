import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';
import { NamingUtil } from '../../utils/naming';

/**
 * Standard Lambda construct for Python, Node.js, Java, etc.
 * Uses runtime + handler + code from asset/inline/S3
 *
 * Resource names should match parameter file configuration:
 * Pattern: <tenant_id>-<env_code>-<region_code>-<functionality>
 * Example: acme-d-use1-order-processor
 */
export interface StandardLambdaProps {
  functionName: string;
  description?: string; // Optional description for the Lambda function
  runtime: lambda.Runtime;
  handler: string;
  code: lambda.Code;
  environment?: { [key: string]: string };
  timeout?: cdk.Duration;
  memorySize?: number;
  role?: cdk.aws_iam.IRole; // Optional existing IAM role
  vpc?: ec2.IVpc;
  vpcSubnets?: ec2.SubnetSelection;
  securityGroups?: ec2.ISecurityGroup[];
  naming?: NamingUtil; // Optional validator
  validateNaming?: boolean; // Default: true if naming provided
  /**
   * Enable Lambda tenant isolation mode (PER_TENANT).
   * Each tenant gets a dedicated execution environment.
   * NOTE: Cannot be enabled on existing functions — only on new function creation.
   */
  tenantIsolation?: boolean;
  /** CloudWatch log retention in days. Default: 30 */
  logRetentionDays?: number;
  /** Removal policy for the log group. Default: DESTROY */
  logRemovalPolicy?: cdk.RemovalPolicy;
  /** KMS key for log group encryption */
  logEncryptionKey?: kms.IKey;
}

export class StandardLambda extends Construct {
  public readonly function: lambda.Function;
  public readonly functionArn: string;
  public readonly logGroup: logs.LogGroup;

  constructor(scope: Construct, id: string, props: StandardLambdaProps) {
    super(scope, id);

    // Validate naming if validator provided
    if (props.naming && props.validateNaming !== false) {
      const validation = props.naming.validateResourceName(props.functionName);
      if (!validation.isValid) {
        console.warn(`Lambda function naming validation warnings for "${props.functionName}":`);
        validation.errors.forEach((error) => console.warn(`  - ${error}`));
      }
    }

    // Explicit log group with configurable retention and removal policy
    this.logGroup = new logs.LogGroup(this, 'LogGroup', {
      logGroupName: `/aws/lambda/${props.functionName}`,
      retention: this.resolveRetention(props.logRetentionDays ?? 30),
      removalPolicy: props.logRemovalPolicy ?? cdk.RemovalPolicy.DESTROY,
      ...(props.logEncryptionKey ? { encryptionKey: props.logEncryptionKey } : {}),
    });

    // Child ID 'Fn' keeps logical IDs concise:
    // e.g. BLambdaGetConfig/Fn → BLambdaGetConfigFn...
    this.function = new lambda.Function(this, 'Fn', {
      functionName: props.functionName,
      description: props.description,
      runtime: props.runtime,
      handler: props.handler,
      code: props.code,
      environment: props.environment,
      timeout: props.timeout || cdk.Duration.seconds(30),
      memorySize: props.memorySize || 128,
      logGroup: this.logGroup,
      ...(props.role ? { role: props.role } : {}),
      ...(props.vpc ? { vpc: props.vpc } : {}),
      ...(props.vpcSubnets ? { vpcSubnets: props.vpcSubnets } : {}),
      ...(props.securityGroups ? { securityGroups: props.securityGroups } : {}),
      ...(props.tenantIsolation ? { tenancyConfig: lambda.TenancyConfig.PER_TENANT } : {}),
    });

    this.functionArn = this.function.functionArn;

    // Create SSM parameter if naming util provided
    if (props.naming) {
      new ssm.StringParameter(this, 'ArnParameter', {
        parameterName: props.naming.generateSsmPath('lambda', id),
        stringValue: this.functionArn,
        description: `ARN for Lambda function ${props.functionName}`,
      });
    }
  }

  private resolveRetention(days: number): logs.RetentionDays {
    const mapping: Record<number, logs.RetentionDays> = {
      1: logs.RetentionDays.ONE_DAY,
      3: logs.RetentionDays.THREE_DAYS,
      5: logs.RetentionDays.FIVE_DAYS,
      7: logs.RetentionDays.ONE_WEEK,
      14: logs.RetentionDays.TWO_WEEKS,
      30: logs.RetentionDays.ONE_MONTH,
      60: logs.RetentionDays.TWO_MONTHS,
      90: logs.RetentionDays.THREE_MONTHS,
      120: logs.RetentionDays.FOUR_MONTHS,
      150: logs.RetentionDays.FIVE_MONTHS,
      180: logs.RetentionDays.SIX_MONTHS,
      365: logs.RetentionDays.ONE_YEAR,
      400: logs.RetentionDays.THIRTEEN_MONTHS,
      545: logs.RetentionDays.EIGHTEEN_MONTHS,
      731: logs.RetentionDays.TWO_YEARS,
      1827: logs.RetentionDays.FIVE_YEARS,
      3653: logs.RetentionDays.TEN_YEARS,
    };
    return mapping[days] ?? logs.RetentionDays.ONE_MONTH;
  }
}
