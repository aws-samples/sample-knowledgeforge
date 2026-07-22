import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';
import { NamingUtil } from '../../utils/naming';

/**
 * Lambda construct with Layer support for shared dependencies
 * Layers are mounted at /opt in the Lambda execution environment
 *
 * Resource names should match parameter file configuration:
 * Pattern: <tenant_id>-<env_code>-<region_code>-<functionality>
 * Example: acme-d-use1-data-processor
 */
export interface LayeredLambdaProps {
  functionName: string;
  runtime: lambda.Runtime;
  handler: string;
  code: lambda.Code;
  layerCode: lambda.Code;
  layerDescription?: string;
  environment?: { [key: string]: string };
  timeout?: cdk.Duration;
  memorySize?: number;
  vpc?: ec2.IVpc;
  vpcSubnets?: ec2.SubnetSelection;
  securityGroups?: ec2.ISecurityGroup[];
  naming?: NamingUtil; // Optional validator
  validateNaming?: boolean; // Default: true if naming provided
  /** CloudWatch log retention in days. Default: 30 */
  logRetentionDays?: number;
  /** Removal policy for the log group. Default: DESTROY */
  logRemovalPolicy?: cdk.RemovalPolicy;
  /** KMS key for log group encryption */
  logEncryptionKey?: kms.IKey;
}

export class LayeredLambda extends Construct {
  public readonly function: lambda.Function;
  public readonly layer: lambda.LayerVersion;
  public readonly functionArn: string;
  public readonly logGroup: logs.LogGroup;

  constructor(scope: Construct, id: string, props: LayeredLambdaProps) {
    super(scope, id);

    // Validate naming if validator provided
    if (props.naming && props.validateNaming !== false) {
      const validation = props.naming.validateResourceName(props.functionName);
      if (!validation.isValid) {
        console.warn(`Layered Lambda naming validation warnings for "${props.functionName}":`);
        validation.errors.forEach((error) => console.warn(`  - ${error}`));
      }
    }

    const retentionDays = props.logRetentionDays ?? 30;
    const retentionMapping: Record<number, logs.RetentionDays> = {
      1: logs.RetentionDays.ONE_DAY, 3: logs.RetentionDays.THREE_DAYS,
      5: logs.RetentionDays.FIVE_DAYS, 7: logs.RetentionDays.ONE_WEEK,
      14: logs.RetentionDays.TWO_WEEKS, 30: logs.RetentionDays.ONE_MONTH,
      60: logs.RetentionDays.TWO_MONTHS, 90: logs.RetentionDays.THREE_MONTHS,
      180: logs.RetentionDays.SIX_MONTHS, 365: logs.RetentionDays.ONE_YEAR,
    };

    this.logGroup = new logs.LogGroup(this, 'LogGroup', {
      logGroupName: `/aws/lambda/${props.functionName}`,
      retention: retentionMapping[retentionDays] ?? logs.RetentionDays.ONE_MONTH,
      removalPolicy: props.logRemovalPolicy ?? cdk.RemovalPolicy.DESTROY,
      ...(props.logEncryptionKey ? { encryptionKey: props.logEncryptionKey } : {}),
    });

    this.layer = new lambda.LayerVersion(this, 'Layer', {
      code: props.layerCode,
      compatibleRuntimes: [props.runtime],
      description: props.layerDescription || 'Lambda layer',
    });

    this.function = new lambda.Function(this, 'Function', {
      functionName: props.functionName,
      runtime: props.runtime,
      handler: props.handler,
      code: props.code,
      layers: [this.layer],
      environment: props.environment,
      timeout: props.timeout || cdk.Duration.seconds(30),
      memorySize: props.memorySize || 128,
      logGroup: this.logGroup,
      ...(props.vpc ? { vpc: props.vpc } : {}),
      ...(props.vpcSubnets ? { vpcSubnets: props.vpcSubnets } : {}),
      ...(props.securityGroups ? { securityGroups: props.securityGroups } : {}),
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
}
