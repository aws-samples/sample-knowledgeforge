# Lambda Constructs

Three Lambda constructs covering standard runtimes, Docker images, and layer-backed functions.
All three create an explicit CloudWatch Log Group with configurable retention and removal policy.

---

## StandardLambda

Standard Lambda construct for Python, Node.js, Java, etc. Uses runtime + handler + code from asset/inline/S3.

### Usage

```typescript
import { StandardLambda } from '@kbanalytics/cdk-constructs';

const fn = new StandardLambda(this, 'OrderProcessor', {
  functionName: 'acme-d-use1-order-processor',
  runtime: lambda.Runtime.PYTHON_3_12,
  handler: 'index.handler',
  code: lambda.Code.fromAsset('lambda/order-processor'),
  environment: { TABLE_NAME: table.tableName },
  timeout: cdk.Duration.seconds(60),
  memorySize: 256,
  logRetentionDays: 90,
  logRemovalPolicy: cdk.RemovalPolicy.DESTROY,
  logEncryptionKey: kmsKey,
});
```

### Props (`StandardLambdaProps`)

| Prop | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| functionName | `string` | Yes | - | Lambda function name. Pattern: `<tenant>-<env>-<region>-<functionality>` |
| runtime | `lambda.Runtime` | Yes | - | Lambda runtime (e.g. `PYTHON_3_12`, `NODEJS_20_X`) |
| handler | `string` | Yes | - | Entry point (e.g. `index.handler`) |
| code | `lambda.Code` | Yes | - | Function code from asset, S3, or inline |
| environment | `{ [key: string]: string }` | No | - | Environment variables |
| timeout | `cdk.Duration` | No | 30s | Function timeout |
| memorySize | `number` | No | 128 | Memory in MB |
| role | `iam.IRole` | No | - | Existing IAM execution role |
| vpc | `ec2.IVpc` | No | - | VPC for the function |
| vpcSubnets | `ec2.SubnetSelection` | No | - | Subnet selection within VPC |
| securityGroups | `ec2.ISecurityGroup[]` | No | - | Security groups for VPC-connected function |
| naming | `NamingUtil` | No | - | Naming validator; also creates SSM parameter for the function ARN |
| validateNaming | `boolean` | No | `true` (if naming set) | Whether to validate the function name |
| tenantIsolation | `boolean` | No | `false` | Enable `PER_TENANT` tenancy config. Only for new functions. |
| logRetentionDays | `number` | No | 30 | CloudWatch log retention in days (1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1827, 3653) |
| logRemovalPolicy | `cdk.RemovalPolicy` | No | `DESTROY` | What happens to the log group when the stack is deleted |
| logEncryptionKey | `kms.IKey` | No | - | KMS key for log group encryption |

### Properties (read-only)

| Property | Type | Description |
|----------|------|-------------|
| function | `lambda.Function` | The underlying CDK Lambda Function |
| functionArn | `string` | ARN of the Lambda function |
| logGroup | `logs.LogGroup` | The explicit CloudWatch Log Group |

---

## DockerLambda

Docker-based Lambda construct for custom container images (Dockerfile or ECR).

### Usage

```typescript
import { DockerLambda } from '@kbanalytics/cdk-constructs';

const fn = new DockerLambda(this, 'ImageProcessor', {
  functionName: 'acme-d-use1-image-processor',
  code: lambda.DockerImageCode.fromImageAsset('docker/image-processor'),
  timeout: cdk.Duration.minutes(5),
  memorySize: 1024,
  logRetentionDays: 14,
});
```

### Props (`DockerLambdaProps`)

| Prop | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| functionName | `string` | Yes | - | Lambda function name |
| code | `lambda.DockerImageCode` | Yes | - | Docker image code (from asset or ECR) |
| environment | `{ [key: string]: string }` | No | - | Environment variables |
| timeout | `cdk.Duration` | No | 30s | Function timeout |
| memorySize | `number` | No | 128 | Memory in MB |
| vpc | `ec2.IVpc` | No | - | VPC for the function |
| vpcSubnets | `ec2.SubnetSelection` | No | - | Subnet selection within VPC |
| securityGroups | `ec2.ISecurityGroup[]` | No | - | Security groups |
| naming | `NamingUtil` | No | - | Naming validator; creates SSM parameter |
| validateNaming | `boolean` | No | `true` (if naming set) | Whether to validate the function name |
| logRetentionDays | `number` | No | 30 | CloudWatch log retention in days |
| logRemovalPolicy | `cdk.RemovalPolicy` | No | `DESTROY` | What happens to the log group when the stack is deleted |
| logEncryptionKey | `kms.IKey` | No | - | KMS key for log group encryption |

### Properties (read-only)

| Property | Type | Description |
|----------|------|-------------|
| function | `lambda.DockerImageFunction` | The underlying CDK Docker image function |
| functionArn | `string` | ARN of the Lambda function |
| logGroup | `logs.LogGroup` | The explicit CloudWatch Log Group |

---

## LayeredLambda

Lambda construct with a Lambda Layer for shared dependencies. Layers are mounted at `/opt` in the execution environment.

### Usage

```typescript
import { LayeredLambda } from '@kbanalytics/cdk-constructs';

const fn = new LayeredLambda(this, 'DataProcessor', {
  functionName: 'acme-d-use1-data-processor',
  runtime: lambda.Runtime.PYTHON_3_12,
  handler: 'index.handler',
  code: lambda.Code.fromAsset('lambda/data-processor'),
  layerCode: lambda.Code.fromAsset('lambda/layers/shared-deps'),
  layerDescription: 'Shared Python dependencies',
  logRetentionDays: 90,
  logRemovalPolicy: cdk.RemovalPolicy.RETAIN,
});
```

### Props (`LayeredLambdaProps`)

| Prop | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| functionName | `string` | Yes | - | Lambda function name |
| runtime | `lambda.Runtime` | Yes | - | Lambda runtime |
| handler | `string` | Yes | - | Entry point |
| code | `lambda.Code` | Yes | - | Function code |
| layerCode | `lambda.Code` | Yes | - | Code for the Lambda Layer |
| layerDescription | `string` | No | `'Lambda layer'` | Description for the layer |
| environment | `{ [key: string]: string }` | No | - | Environment variables |
| timeout | `cdk.Duration` | No | 30s | Function timeout |
| memorySize | `number` | No | 128 | Memory in MB |
| vpc | `ec2.IVpc` | No | - | VPC for the function |
| vpcSubnets | `ec2.SubnetSelection` | No | - | Subnet selection within VPC |
| securityGroups | `ec2.ISecurityGroup[]` | No | - | Security groups |
| naming | `NamingUtil` | No | - | Naming validator; creates SSM parameter |
| validateNaming | `boolean` | No | `true` (if naming set) | Whether to validate the function name |
| logRetentionDays | `number` | No | 30 | CloudWatch log retention in days |
| logRemovalPolicy | `cdk.RemovalPolicy` | No | `DESTROY` | What happens to the log group when the stack is deleted |
| logEncryptionKey | `kms.IKey` | No | - | KMS key for log group encryption |

### Properties (read-only)

| Property | Type | Description |
|----------|------|-------------|
| function | `lambda.Function` | The underlying CDK Lambda Function |
| layer | `lambda.LayerVersion` | The created Lambda Layer |
| functionArn | `string` | ARN of the Lambda function |
| logGroup | `logs.LogGroup` | The explicit CloudWatch Log Group |

---

## Config-Driven Usage

In `kbanalytics-infra` YAML config, Lambda functions support log group settings at both global and per-lambda level:

```yaml
# Global defaults (top-level)
log_retention_days: 30
log_removal_policy: "destroy"

shared_lambdas:
  order_processor:
    name: "order_processor"
    runtime: python3.12
    handler: index.handler
    code_path: ./lambda/order_processor
    timeout: 60
    memory: 256
    # Per-lambda override (optional)
    log_retention_days: 90
    log_removal_policy: "retain"
```

Per-lambda values override the global defaults. If neither is set, the construct defaults to 30 days retention with DESTROY removal policy.

## Notes

- All three constructs create an explicit CloudWatch Log Group at `/aws/lambda/<functionName>` - Lambda does not auto-create one.
- Log groups are created before the Lambda function to avoid race conditions.
- All three constructs auto-create an SSM parameter (`/<prefix>/lambda/<id>/arn`) when a `NamingUtil` is provided.
- Resource naming follows the pattern `<tenant_id>-<env_code>-<region_code>-<functionality>`.
- `tenantIsolation` on `StandardLambda` cannot be enabled on existing functions - only on new function creation.
- `DockerLambda` does not accept `runtime` or `handler` - these are baked into the container image.
