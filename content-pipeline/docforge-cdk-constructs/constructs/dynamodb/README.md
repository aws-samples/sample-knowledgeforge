# CustomDynamoDB

DynamoDB Table construct with optional KMS encryption and naming validation.

## Usage

```typescript
import { CustomDynamoDB } from '@docforge/cdk-constructs';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';

const table = new CustomDynamoDB(this, 'SessionTable', {
  tableName: 'orgAlpha-d-use1-session-data',
  partitionKey: { name: 'pk', type: dynamodb.AttributeType.STRING },
  sortKey: { name: 'sk', type: dynamodb.AttributeType.STRING },
  encryptionKey: kmsKey.key,
});
```

## Props

### `CustomDynamoDBProps`

| Prop | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| tableName | `string` | Yes | - | Table name. Pattern: `<tenant>-<env>-<region>-<functionality>` |
| partitionKey | `dynamodb.Attribute` | Yes | - | Partition key definition (`{ name, type }`) |
| sortKey | `dynamodb.Attribute` | No | - | Sort key definition |
| billingMode | `dynamodb.BillingMode` | No | `PAY_PER_REQUEST` | Billing mode (`PAY_PER_REQUEST` or `PROVISIONED`) |
| removalPolicy | `cdk.RemovalPolicy` | No | `RETAIN` | Removal policy on stack deletion |
| encryptionKey | `kms.IKey` | No | - | Customer-managed KMS key. If omitted, uses AWS-owned key. |
| naming | `NamingUtil` | No | - | Naming validator; creates SSM parameters for table ARN and name |
| validateNaming | `boolean` | No | `true` (if naming set) | Whether to validate the table name |

## Properties (read-only)

| Property | Type | Description |
|----------|------|-------------|
| table | `dynamodb.Table` | The underlying CDK DynamoDB Table |
| tableArn | `string` | ARN of the table |

## Config-Driven Usage

```yaml
dynamodb_tables:
  session-data:
    table_name: "orgAlpha-d-use1-session-data"
    partition_key:
      name: pk
      type: S
    sort_key:
      name: sk
      type: S
    encryption_key_arn: "arn:aws:kms:..."
```

## Notes

- Default `RemovalPolicy` is `RETAIN` - tables are preserved on stack deletion.
- Default `BillingMode` is `PAY_PER_REQUEST` (on-demand).
- When `encryptionKey` is provided, encryption is set to `CUSTOMER_MANAGED`. Otherwise DynamoDB uses its default AWS-owned encryption.
- When a `NamingUtil` is provided, two SSM parameters are created: one for the table ARN and one for the table name.
