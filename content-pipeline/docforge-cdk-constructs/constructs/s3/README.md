# S3BucketConstruct

Creates an S3 bucket with enforced public access blocking, SSL enforcement, and optional KMS encryption.

## Usage

```typescript
import { S3BucketConstruct } from '@docforge/cdk-constructs';

const bucket = new S3BucketConstruct(this, 'DataBucket', {
  bucketName: 'orgAlpha-dev-data-bucket',
  versioning: true,
  encryption: true,
  kmsKey: kmsKey.key,
  tags: { Environment: 'dev' },
});
```

## Props

### `S3BucketConfig`

| Prop | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| bucketName | `string` | Yes | - | Bucket name |
| versioning | `boolean` | Yes | - | Enable versioning |
| encryption | `boolean` | Yes | - | Enable S3-managed encryption (when no `kmsKey`) |
| kmsKey | `kms.IKey` | No | - | Customer-managed KMS key. Overrides `encryption` to KMS mode. |
| tags | `Record<string, string>` | Yes | - | Tags to apply |

## Properties (read-only)

| Property | Type | Description |
|----------|------|-------------|
| bucket | `s3.IBucket` | The created S3 Bucket |

## Config-Driven Usage

```yaml
s3_buckets:
  data-bucket:
    bucket_name: "orgAlpha-dev-data-bucket"
    versioning: true
    encryption: true
    kms_key_arn: "arn:aws:kms:..."
    tags:
      Environment: dev
```

## Notes

- `RemovalPolicy` is set to `RETAIN` - buckets are preserved on stack deletion.
- Public access is fully blocked (`BlockPublicAccess.BLOCK_ALL` equivalent).
- SSL is enforced via `enforceSSL: true`.
- Encryption logic: if `kmsKey` is provided → KMS encryption; else if `encryption: true` → S3-managed encryption; else unencrypted.
