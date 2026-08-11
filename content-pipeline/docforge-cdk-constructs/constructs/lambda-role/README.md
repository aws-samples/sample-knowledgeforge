# LambdaRoleConstruct

Creates an IAM role for Lambda execution with optional KMS decrypt permissions and custom inline policies. Designed to be driven entirely from YAML config.

## Usage

```typescript
import { LambdaRoleConstruct } from '@docforge/cdk-constructs';

const lambdaRole = new LambdaRoleConstruct(this, 'MyLambdaRole', {
  roleName: 'orgAlpha-d-use1-order-processor-role',
  functionName: 'orgAlpha-d-use1-order-processor',
  kmsDecryptKeyArn: 'arn:aws:kms:REGION:ACCOUNT_ID:key/KEY_ID',
  policies: [
    {
      actions: ['s3:GetObject'],
      resources: ['arn:aws:s3:::my-bucket/*'],
    },
    {
      actions: ['dynamodb:Query', 'dynamodb:GetItem'],
      resources: ['arn:aws:dynamodb:us-east-1:000000000012:table/my-table'],
    },
  ],
  tags: { Environment: 'dev' },
});
```

## Props

### `LambdaRoleConfig`

| Prop | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| roleName | `string` | No | CDK auto-generated | Explicit IAM role name |
| kmsDecryptKeyArn | `string` | No | - | KMS key ARN (or alias like `alias/my-key`) to grant `kms:Decrypt`. Scoped to `functionName` via condition if provided. |
| functionName | `string` | No | - | Lambda function name - used for KMS condition key and log group scoping |
| policies | `PolicyStatementConfig[]` | No | - | Additional inline policy statements |
| tags | `Record<string, string>` | No | - | Tags to apply to the role |

### `PolicyStatementConfig`

| Prop | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| actions | `string[]` | Yes | - | IAM actions (e.g. `['s3:GetObject']`) |
| resources | `string[]` | Yes | - | Resource ARNs the actions apply to |
| effect | `'Allow' \| 'Deny'` | No | `'Allow'` | Statement effect |

## Properties (read-only)

| Property | Type | Description |
|----------|------|-------------|
| role | `iam.Role` | The created IAM Role. Includes `AWSLambdaBasicExecutionRole` managed policy. |

## Config-Driven Usage

```yaml
role:
  name: "orgAlpha-d-use1-order-processor-role"
  function_name: "orgAlpha-d-use1-order-processor"
  kms_decrypt_key_arn: "arn:aws:kms:REGION:ACCOUNT_ID:key/KEY_ID"
  policies:
    - actions: ["s3:GetObject"]
      resources: ["arn:aws:s3:::my-bucket/*"]
    - actions: ["dynamodb:Query"]
      resources: ["arn:aws:dynamodb:us-east-1:000000000012:table/my-table"]
  tags:
    Environment: dev
```

## Notes

- The role always includes the `AWSLambdaBasicExecutionRole` managed policy.
- If `kmsDecryptKeyArn` starts with `alias/`, it is automatically converted to a full ARN using the stack's region and account.
- KMS decrypt is scoped via a `kms:EncryptionContext:LambdaFunctionName` condition when `functionName` is provided.
