import * as cdk from 'aws-cdk-lib';
import * as s3vectors from 'aws-cdk-lib/aws-s3vectors';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';
import { NamingUtil } from '../../utils/naming';

/**
 * S3 Vectors construct.
 *
 * Creates an S3 Vector Bucket and Index for storing and querying embeddings.
 *
 * Pattern: <tenant_id>-<env_code>-<region_code>-doc-vectors
 * Example: example-s-euw1-doc-vectors
 */
export interface S3VectorsConstructProps {
  /** Vector bucket name */
  vectorBucketName: string;
  /** Vector index name */
  indexName: string;
  /** Embedding dimensions (e.g. 1024 for Titan Embed V2) */
  dimension: number;
  /** Distance metric for similarity search */
  distanceMetric?: string;
  /** Data type for vectors */
  dataType?: string;
  /** Optional NamingUtil for validation */
  naming?: NamingUtil;
  /** Optional SSM prefix for storing bucket and index names */
  ssmPrefix?: string;
}

export class S3VectorsConstruct extends Construct {
  public readonly vectorBucketName: string;
  public readonly indexName: string;

  constructor(scope: Construct, id: string, props: S3VectorsConstructProps) {
    super(scope, id);

    if (props.naming) {
      const validation = props.naming.validateResourceName(props.vectorBucketName);
      if (!validation.isValid) {
        console.warn(`S3 Vectors naming validation warnings for "${props.vectorBucketName}":`);
        validation.errors.forEach((e) => console.warn(`  - ${e}`));
      }
    }

    const bucket = new s3vectors.CfnVectorBucket(this, 'VectorBucket', {
      vectorBucketName: props.vectorBucketName,
    });
    bucket.applyRemovalPolicy(cdk.RemovalPolicy.RETAIN);

    const index = new s3vectors.CfnIndex(this, 'VectorIndex', {
      vectorBucketName: props.vectorBucketName,
      indexName: props.indexName,
      dimension: props.dimension,
      distanceMetric: props.distanceMetric || 'cosine',
      dataType: props.dataType || 'float32',
    });
    index.addDependency(bucket);
    index.applyRemovalPolicy(cdk.RemovalPolicy.RETAIN);

    this.vectorBucketName = props.vectorBucketName;
    this.indexName = props.indexName;

    if (props.ssmPrefix) {
      new ssm.StringParameter(this, 'BucketNameParam', {
        parameterName: `${props.ssmPrefix}/s3-vectors/${id}/bucket-name`,
        stringValue: props.vectorBucketName,
        description: `S3 Vectors bucket name: ${props.vectorBucketName}`,
      });
      new ssm.StringParameter(this, 'IndexNameParam', {
        parameterName: `${props.ssmPrefix}/s3-vectors/${id}/index-name`,
        stringValue: props.indexName,
        description: `S3 Vectors index name: ${props.indexName}`,
      });
    }
  }
}
