# Lambda Layer Construct

Standalone Lambda Layer construct for creating reusable shared dependency layers that can be attached to multiple Lambda functions.

---

## LambdaLayer

Creates a Lambda Layer Version from a local asset directory, S3 bucket, or inline code. Unlike `LayeredLambda` (which couples a layer with a single function), this construct creates a standalone layer that can be shared across any number of functions.

### Usage

```typescript
import { LambdaLayer } from '@docforge/cdk-constructs';

// From local directory (auto-zipped by CDK)
const layer = new LambdaLayer(this, 'SharedDeps', {
  layerName: 'shared-d-use1-teams-shared-deps',
  description: 'Shared Python dependencies for Teams middle layer',
  code: lambda.Code.fromAsset('lambda/layers/shared-deps'),
  compatibleRuntimes: [lambda.Runtime.PYTHON_3_12],
});

// From S3 bucket
const s3Layer = new LambdaLayer(this, 'S3Layer', {
  layerName: 'shared-d-use1-ml-deps',
  description: 'ML dependencies from deployment bucket',
  s3Bucket: 'my-deployment-bucket',
  s3Key: 'layers/ml-deps.zip',
  compatibleRuntimes: [lambda.Runtime.PYTHON_3_12],
});

// Attach to Lambda functions
new lambda.Function(this, 'MyFunction', {
  // ...
  layers: [layer.layerVersion],
});
```

### Props (`LambdaLayerProps`)

| Prop | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| layerName | `string` | Yes | - | Layer version name. Pattern: `<tenant>-<env>-<region>-<name>` |
| description | `string` | No | `'Lambda layer: <name>'` | Human-readable description |
| code | `lambda.Code` | No* | - | Layer code from asset, S3, or inline |
| s3Bucket | `string` | No* | - | S3 bucket name containing the layer zip |
| s3Key | `string` | No** | - | S3 object key for the layer zip |
| compatibleRuntimes | `lambda.Runtime[]` | No | `[PYTHON_3_12]` | Compatible Lambda runtimes |
| license | `string` | No | - | License info (e.g. `'MIT'`, `'Apache-2.0'`) |
| naming | `NamingUtil` | No | - | Naming validator; creates SSM parameter for layer ARN |
| validateNaming | `boolean` | No | `true` (if naming set) | Whether to validate the layer name |
| removalPolicy | `cdk.RemovalPolicy` | No | `RETAIN` | What happens when the layer is removed from the stack |

\* Either `code` or `s3Bucket` + `s3Key` must be provided.
\** Required when `s3Bucket` is set.

### Properties (read-only)

| Property | Type | Description |
|----------|------|-------------|
| layerVersion | `lambda.LayerVersion` | The underlying CDK LayerVersion |
| layerVersionArn | `string` | ARN of the layer version (includes version number) |

---

## Config-Driven Usage

In `docforge-infra` YAML config, layers can be defined under a `lambda_layers` key:

```yaml
# From local asset directory
lambda_layers:
  shared_deps:
    name: "teams-shared-deps"
    description: "Shared Python dependencies for Teams middle layer"
    code_path: ./lambda/layers/shared-deps
    compatible_runtimes: [python3.12]

# From S3 deployment bucket
lambda_layers:
  shared_deps:
    name: "teams-shared-deps"
    description: "Shared Python dependencies"
    s3_bucket: "my-deployment-bucket"
    s3_key: "middle-layer/shared-layer.zip"
    compatible_runtimes: [python3.12]
```

The stack code reads this config and creates the layer:

```typescript
const layerCfg = config.lambda_layers?.shared_deps;
if (layerCfg) {
  const layer = new LambdaLayer(this, 'SharedDeps', {
    layerName: naming.resolveName({ name: layerCfg.name }),
    description: layerCfg.description,
    ...(layerCfg.s3_bucket
      ? { s3Bucket: layerCfg.s3_bucket, s3Key: layerCfg.s3_key }
      : { code: lambda.Code.fromAsset(layerCfg.code_path) }),
    compatibleRuntimes: (layerCfg.compatible_runtimes || ['python3.12'])
      .map((r: string) => new lambda.Runtime(r)),
  });
  // Attach to functions: layers: [layer.layerVersion]
}
```

## Difference from LayeredLambda

| | LambdaLayer | LayeredLambda |
|---|---|---|
| Creates | Layer only | Layer + Function (coupled) |
| Reusable | Yes - attach to any number of functions | No - one layer per function |
| Use case | Shared deps across multiple lambdas | Single function with private deps |

## Notes

- Layer code directory must follow the [Lambda layer packaging structure](https://docs.aws.amazon.com/lambda/latest/dg/packaging-layers.html): `python/` subdirectory for Python dependencies.
- CDK auto-zips local directories during `cdk deploy` - no manual zipping needed.
- SSM parameter is created at `/<prefix>/lambda-layer/<id>/arn` when `NamingUtil` is provided.
- Default removal policy is `RETAIN` to prevent accidental deletion of layers used by deployed functions.
