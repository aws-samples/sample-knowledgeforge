import * as cdk from 'aws-cdk-lib';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import * as kms from 'aws-cdk-lib/aws-kms';
import { Construct } from 'constructs';

export interface S3BucketConfig {
  bucketName: string;
  versioning: boolean;
  encryption: boolean;
  kmsKey?: kms.IKey;
  tags: Record<string, string>;
  /** If provided, creates an SSM parameter with the bucket name at this path */
  ssmPrefix?: string;
  /** Import existing bucket by name instead of creating new one */
  importExisting?: boolean;
}

/**
 * S3 Bucket Construct
 *
 * Creates:
 * - S3 bucket with configurable versioning and encryption (S3-managed or KMS)
 * - Public access block (all public access blocked)
 * - SSL enforcement policy
 * - SSM parameter for bucket name (if ssmPrefix provided)
 */
export class S3BucketConstruct extends Construct {
  public readonly bucket: s3.IBucket;

  constructor(scope: Construct, id: string, config: S3BucketConfig) {
    super(scope, id);

    const encryptionType = config.kmsKey
      ? s3.BucketEncryption.KMS
      : config.encryption
        ? s3.BucketEncryption.S3_MANAGED
        : s3.BucketEncryption.UNENCRYPTED;

    const bucket = config.importExisting
      ? s3.Bucket.fromBucketName(this, 'Bucket', config.bucketName)
      : new s3.Bucket(this, 'Bucket', {
          bucketName: config.bucketName,
          versioned: config.versioning,
          encryption: encryptionType,
          encryptionKey: config.kmsKey,
          blockPublicAccess: new s3.BlockPublicAccess({
            blockPublicAcls: true,
            blockPublicPolicy: true,
            ignorePublicAcls: true,
            restrictPublicBuckets: true,
          }),
          enforceSSL: true,
          removalPolicy: cdk.RemovalPolicy.RETAIN,
        });

    // Apply tags only when creating new bucket
    if (!config.importExisting) {
      Object.entries(config.tags).forEach(([key, value]) => {
        cdk.Tags.of(bucket).add(key, value);
      });
    }

    this.bucket = bucket;

    // SSM parameter for bucket name
    if (config.ssmPrefix) {
      new ssm.StringParameter(this, 'BucketNameParam', {
        parameterName: `${config.ssmPrefix}/bucket-name`,
        stringValue: bucket.bucketName,
        description: `S3 bucket name: ${config.bucketName}`,
      });
    }
  }
}
