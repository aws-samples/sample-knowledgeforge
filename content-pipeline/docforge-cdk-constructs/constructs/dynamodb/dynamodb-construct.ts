import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';
import { NamingUtil } from '../../utils/naming';

/**
 * DynamoDB Table construct
 *
 * Resource names should match parameter file configuration:
 * Pattern: <tenant_id>-<env_code>-<region_code>-<functionality>
 * Example: orgAlpha-d-use1-session-data
 */
export interface CustomDynamoDBProps {
  tableName: string;
  partitionKey: dynamodb.Attribute;
  sortKey?: dynamodb.Attribute;
  billingMode?: dynamodb.BillingMode;
  removalPolicy?: cdk.RemovalPolicy;
  /** Optional customer-managed KMS key for encryption. If omitted, uses AWS-owned key. */
  encryptionKey?: cdk.aws_kms.IKey;
  /** Enable DynamoDB Streams with the specified view type */
  stream?: dynamodb.StreamViewType;
  /** Enable Point-in-Time Recovery. Default: false */
  pointInTimeRecovery?: boolean;
  naming?: NamingUtil; // Optional validator
  validateNaming?: boolean; // Default: true if naming provided
}

export class CustomDynamoDB extends Construct {
  public readonly table: dynamodb.Table;
  public readonly tableArn: string;

  constructor(scope: Construct, id: string, props: CustomDynamoDBProps) {
    super(scope, id);

    // Validate naming if validator provided
    if (props.naming && props.validateNaming !== false) {
      const validation = props.naming.validateResourceName(props.tableName);
      if (!validation.isValid) {
        console.warn(`DynamoDB table naming validation warnings for "${props.tableName}":`);
        validation.errors.forEach((error) => console.warn(`  - ${error}`));
      }
    }

    this.table = new dynamodb.Table(this, 'Table', {
      tableName: props.tableName,
      partitionKey: props.partitionKey,
      sortKey: props.sortKey,
      billingMode: props.billingMode || dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: props.removalPolicy || cdk.RemovalPolicy.RETAIN,
      ...(props.stream ? { stream: props.stream } : {}),
      ...(props.pointInTimeRecovery ? { pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true } } : {}),
      ...(props.encryptionKey
        ? { encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED, encryptionKey: props.encryptionKey }
        : {}),
    });

    this.tableArn = this.table.tableArn;

    // Create SSM parameters if naming util provided
    if (props.naming) {
      new ssm.StringParameter(this, 'ArnParameter', {
        parameterName: props.naming.generateSsmPath('dynamodb', id),
        stringValue: this.tableArn,
        description: `ARN for DynamoDB table ${props.tableName}`,
      });

      new ssm.StringParameter(this, 'NameParameter', {
        parameterName: props.naming.generateSsmPath('dynamodb', id, 'name'),
        stringValue: this.table.tableName,
        description: `Name for DynamoDB table ${props.tableName}`,
      });
    }
  }
}
