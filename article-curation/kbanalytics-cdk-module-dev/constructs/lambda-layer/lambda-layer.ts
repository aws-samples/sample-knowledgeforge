import * as cdk from 'aws-cdk-lib';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';
import { NamingUtil } from '../../utils/naming';

/**
 * Standalone Lambda Layer construct for shared dependencies.
 *
 * Creates a reusable Lambda Layer that can be attached to multiple functions.
 * Supports code from local asset directory, S3 bucket, or inline specification.
 *
 * Resource names should match parameter file configuration:
 * Pattern: <tenant_id>-<env_code>-<region_code>-<layer_name>
 * Example: shared-d-use1-teams-shared-deps
 */
export interface LambdaLayerProps {
  /** Layer version name. Pattern: <tenant>-<env>-<region>-<name> */
  layerName: string;
  /** Human-readable description of the layer contents */
  description?: string;
  /**
   * Layer code from local asset, S3, or inline.
   * Mutually exclusive with s3Bucket + s3Key.
   * Use `lambda.Code.fromAsset('path/to/layer')` for local directories.
   */
  code?: lambda.Code;
  /** S3 bucket name containing the layer zip (alternative to `code`) */
  s3Bucket?: string;
  /** S3 object key for the layer zip (required when s3Bucket is set) */
  s3Key?: string;
  /** Compatible Lambda runtimes. Defaults to [PYTHON_3_12] */
  compatibleRuntimes?: lambda.Runtime[];
  /** License info for the layer (e.g. 'MIT', 'Apache-2.0') */
  license?: string;
  /** Naming validator; also creates SSM parameter for the layer ARN */
  naming?: NamingUtil;
  /** Whether to validate the layer name. Default: true if naming provided */
  validateNaming?: boolean;
  /** Removal policy. Default: RETAIN */
  removalPolicy?: cdk.RemovalPolicy;
}

export class LambdaLayer extends Construct {
  /** The underlying CDK LayerVersion */
  public readonly layerVersion: lambda.LayerVersion;
  /** ARN of the layer version (includes version number) */
  public readonly layerVersionArn: string;

  constructor(scope: Construct, id: string, props: LambdaLayerProps) {
    super(scope, id);

    if (props.naming && props.validateNaming !== false) {
      const validation = props.naming.validateResourceName(props.layerName);
      if (!validation.isValid) {
        console.warn(`Lambda Layer naming validation warnings for "${props.layerName}":`);
        validation.errors.forEach((error) => console.warn(`  - ${error}`));
      }
    }

    if (!props.code && !props.s3Bucket) {
      throw new Error(
        `LambdaLayer '${props.layerName}': either 'code' or 's3Bucket' + 's3Key' must be provided.`
      );
    }

    if (props.s3Bucket && !props.s3Key) {
      throw new Error(
        `LambdaLayer '${props.layerName}': 's3Key' is required when 's3Bucket' is specified.`
      );
    }

    const layerCode = props.code
      ?? lambda.Code.fromBucket(
           s3.Bucket.fromBucketName(this, 'LayerBucket', props.s3Bucket!),
           props.s3Key!,
         );

    this.layerVersion = new lambda.LayerVersion(this, 'Layer', {
      layerVersionName: props.layerName,
      description: props.description || `Lambda layer: ${props.layerName}`,
      code: layerCode,
      compatibleRuntimes: props.compatibleRuntimes || [lambda.Runtime.PYTHON_3_12],
      license: props.license,
      removalPolicy: props.removalPolicy ?? cdk.RemovalPolicy.RETAIN,
    });

    this.layerVersionArn = this.layerVersion.layerVersionArn;

    if (props.naming) {
      new ssm.StringParameter(this, 'LayerArnParameter', {
        parameterName: props.naming.generateSsmPath('lambda-layer', id),
        stringValue: this.layerVersionArn,
        description: `ARN for Lambda Layer ${props.layerName}`,
      });
    }
  }
}
