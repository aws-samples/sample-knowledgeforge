import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import * as kms from 'aws-cdk-lib/aws-kms';
import { S3BucketConstruct } from '../s3-bucket';

function makeStack() {
  const app = new cdk.App();
  return new cdk.Stack(app, 'TestStack');
}

// S3 bucket names must only contain lowercase letters, numbers, hyphens, and dots.
// The naming convention uses hyphens as separators; underscores are replaced with hyphens for S3.
const VALID_BUCKET_NAME = 'test-d-use1-data-store';
const VALID_ARTIFACT_NAME = 'shared-d-use1-artifact-store';

describe('S3BucketConstruct', () => {
  it('exposes bucket as a public readonly IBucket property', () => {
    const stack = makeStack();
    const construct = new S3BucketConstruct(stack, 'MyBucket', {
      bucketName: VALID_BUCKET_NAME,
      versioning: true,
      encryption: true,
      tags: {},
    });

    expect(construct.bucket).toBeDefined();
    expect(construct.bucket).not.toBeNull();
  });

  it('blocks all public access', () => {
    const stack = makeStack();
    new S3BucketConstruct(stack, 'MyBucket', {
      bucketName: VALID_BUCKET_NAME,
      versioning: true,
      encryption: true,
      tags: {},
    });

    const template = Template.fromStack(stack);
    template.hasResourceProperties('AWS::S3::Bucket', {
      PublicAccessBlockConfiguration: {
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
        IgnorePublicAcls: true,
        RestrictPublicBuckets: true,
      },
    });
  });

  it('enables versioning when versioning: true', () => {
    const stack = makeStack();
    new S3BucketConstruct(stack, 'MyBucket', {
      bucketName: VALID_BUCKET_NAME,
      versioning: true,
      encryption: true,
      tags: {},
    });

    const template = Template.fromStack(stack);
    template.hasResourceProperties('AWS::S3::Bucket', {
      VersioningConfiguration: { Status: 'Enabled' },
    });
  });

  it('does not enable versioning when versioning: false', () => {
    const stack = makeStack();
    new S3BucketConstruct(stack, 'MyBucket', {
      bucketName: VALID_BUCKET_NAME,
      versioning: false,
      encryption: true,
      tags: {},
    });

    const template = Template.fromStack(stack);
    const buckets = template.findResources('AWS::S3::Bucket');
    const bucketProps = Object.values(buckets)[0].Properties;
    expect(bucketProps.VersioningConfiguration).toBeUndefined();
  });

  it('uses S3_MANAGED encryption when encryption: true and no kmsKey', () => {
    const stack = makeStack();
    new S3BucketConstruct(stack, 'MyBucket', {
      bucketName: VALID_BUCKET_NAME,
      versioning: true,
      encryption: true,
      tags: {},
    });

    const template = Template.fromStack(stack);
    template.hasResourceProperties('AWS::S3::Bucket', {
      BucketEncryption: {
        ServerSideEncryptionConfiguration: [
          {
            ServerSideEncryptionByDefault: {
              SSEAlgorithm: 'AES256',
            },
          },
        ],
      },
    });
  });

  it('uses SSE-KMS encryption when kmsKey is provided', () => {
    const stack = makeStack();
    const key = new kms.Key(stack, 'TestKey');

    new S3BucketConstruct(stack, 'MyBucket', {
      bucketName: VALID_BUCKET_NAME,
      versioning: true,
      encryption: true,
      kmsKey: key,
      tags: {},
    });

    const template = Template.fromStack(stack);
    template.hasResourceProperties('AWS::S3::Bucket', {
      BucketEncryption: {
        ServerSideEncryptionConfiguration: [
          {
            ServerSideEncryptionByDefault: {
              SSEAlgorithm: 'aws:kms',
            },
          },
        ],
      },
    });
  });

  it('applies the bucketName from config', () => {
    const stack = makeStack();
    new S3BucketConstruct(stack, 'MyBucket', {
      bucketName: VALID_ARTIFACT_NAME,
      versioning: true,
      encryption: true,
      tags: {},
    });

    const template = Template.fromStack(stack);
    template.hasResourceProperties('AWS::S3::Bucket', {
      BucketName: VALID_ARTIFACT_NAME,
    });
  });

  it('applies tags from config', () => {
    const stack = makeStack();
    new S3BucketConstruct(stack, 'MyBucket', {
      bucketName: VALID_BUCKET_NAME,
      versioning: true,
      encryption: true,
      tags: { Environment: 'd', Tenant: 'shared' },
    });

    const template = Template.fromStack(stack);
    template.hasResourceProperties('AWS::S3::Bucket', {
      Tags: Match.arrayWith([
        { Key: 'Environment', Value: 'd' },
        { Key: 'Tenant', Value: 'shared' },
      ]),
    });
  });

  it('does not create any SSM Parameter Store resources', () => {
    const stack = makeStack();
    new S3BucketConstruct(stack, 'MyBucket', {
      bucketName: VALID_BUCKET_NAME,
      versioning: true,
      encryption: true,
      tags: {},
    });

    const template = Template.fromStack(stack);
    const ssmParams = template.findResources('AWS::SSM::Parameter');
    expect(Object.keys(ssmParams)).toHaveLength(0);
  });
});
