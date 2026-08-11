import * as cdk from 'aws-cdk-lib';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as kms from 'aws-cdk-lib/aws-kms';
import { Construct } from 'constructs';

export interface S3BucketConfig {
    bucketName: string;
    versioning: boolean;
    encryption: boolean;
    kmsKey?: kms.IKey;
    tags: Record<string, string>;
}

export class S3BucketConstruct extends Construct {
    public readonly bucket: s3.IBucket;

    constructor(scope: Construct, id: string, config: S3BucketConfig) {
        super(scope, id);

        const encryptionType = config.kmsKey
            ? s3.BucketEncryption.KMS
            : config.encryption
                ? s3.BucketEncryption.S3_MANAGED
                : s3.BucketEncryption.UNENCRYPTED;

        const bucket = new s3.Bucket(this, 'Bucket', {
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

        // Apply tags
        Object.entries(config.tags).forEach(([key, value]) => {
            cdk.Tags.of(bucket).add(key, value);
        });

        this.bucket = bucket;
    }
}
