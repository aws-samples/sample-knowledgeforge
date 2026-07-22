import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { S3VectorsConstruct } from '../s3-vectors-construct';
import { NamingUtil } from '../../../utils/naming';

describe('S3VectorsConstruct', () => {
  let app: cdk.App;
  let stack: cdk.Stack;
  let naming: NamingUtil;

  beforeEach(() => {
    app = new cdk.App();
    stack = new cdk.Stack(app, 'TestStack');
    naming = new NamingUtil({ tenantId: 'test', envCode: 'd', regionCode: 'use1' });
  });

  test('creates vector bucket and index', () => {
    new S3VectorsConstruct(stack, 'TestVectors', {
      vectorBucketName: 'test-d-use1-kb-vectors',
      indexName: 'kb-vectors-test',
      dimension: 1024,
      naming,
    });

    const template = Template.fromStack(stack);
    template.resourceCountIs('AWS::S3Vectors::VectorBucket', 1);
    template.resourceCountIs('AWS::S3Vectors::Index', 1);
  });

  test('uses cosine distance metric by default', () => {
    new S3VectorsConstruct(stack, 'TestVectors', {
      vectorBucketName: 'test-d-use1-kb-vectors',
      indexName: 'kb-vectors-test',
      dimension: 1024,
    });

    const template = Template.fromStack(stack);
    template.hasResourceProperties('AWS::S3Vectors::Index', {
      DistanceMetric: 'cosine',
      DataType: 'float32',
      Dimension: 1024,
    });
  });

  test('creates SSM parameters when ssmPrefix provided', () => {
    new S3VectorsConstruct(stack, 'TestVectors', {
      vectorBucketName: 'test-d-use1-kb-vectors',
      indexName: 'kb-vectors-test',
      dimension: 1024,
      naming,
      ssmPrefix: '/test-d-use1',
    });

    const template = Template.fromStack(stack);
    template.resourceCountIs('AWS::SSM::Parameter', 2);
  });

  test('exposes bucket and index names', () => {
    const vectors = new S3VectorsConstruct(stack, 'TestVectors', {
      vectorBucketName: 'test-d-use1-kb-vectors',
      indexName: 'kb-vectors-test',
      dimension: 1024,
    });

    expect(vectors.vectorBucketName).toBe('test-d-use1-kb-vectors');
    expect(vectors.indexName).toBe('kb-vectors-test');
  });
});
