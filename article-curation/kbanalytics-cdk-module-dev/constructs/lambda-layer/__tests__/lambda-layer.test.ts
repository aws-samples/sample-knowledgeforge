import * as cdk from 'aws-cdk-lib';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { LambdaLayer } from '../lambda-layer';
import { NamingUtil } from '../../../utils/naming';

function makeStack() {
  const app = new cdk.App();
  return new cdk.Stack(app, 'TestStack');
}

describe('LambdaLayer', () => {
  test('creates layer with inline code and correct properties', () => {
    const stack = makeStack();

    new LambdaLayer(stack, 'SharedDeps', {
      layerName: 'shared-d-use1-shared-deps',
      description: 'Shared Python dependencies',
      code: lambda.Code.fromAsset(__dirname), // use test dir as dummy asset
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_12],
    });

    const template = Template.fromStack(stack);

    template.hasResourceProperties('AWS::Lambda::LayerVersion', {
      LayerName: 'shared-d-use1-shared-deps',
      Description: 'Shared Python dependencies',
      CompatibleRuntimes: ['python3.12'],
    });
  });

  test('defaults to PYTHON_3_12 when no compatibleRuntimes specified', () => {
    const stack = makeStack();

    new LambdaLayer(stack, 'DefaultRuntime', {
      layerName: 'shared-d-use1-default-layer',
      code: lambda.Code.fromAsset(__dirname),
    });

    const template = Template.fromStack(stack);

    template.hasResourceProperties('AWS::Lambda::LayerVersion', {
      CompatibleRuntimes: ['python3.12'],
    });
  });

  test('supports multiple compatible runtimes', () => {
    const stack = makeStack();

    new LambdaLayer(stack, 'MultiRuntime', {
      layerName: 'shared-d-use1-multi-layer',
      code: lambda.Code.fromAsset(__dirname),
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_12, lambda.Runtime.PYTHON_3_11],
    });

    const template = Template.fromStack(stack);

    template.hasResourceProperties('AWS::Lambda::LayerVersion', {
      CompatibleRuntimes: Match.arrayWith(['python3.12', 'python3.11']),
    });
  });

  test('creates SSM parameter when naming is provided', () => {
    const stack = makeStack();
    const naming = new NamingUtil({
      tenantId: 'shared',
      envCode: 'd',
      regionCode: 'use1',
    });

    new LambdaLayer(stack, 'WithSsm', {
      layerName: 'shared-d-use1-shared-deps',
      code: lambda.Code.fromAsset(__dirname),
      naming,
    });

    const template = Template.fromStack(stack);

    template.hasResourceProperties('AWS::SSM::Parameter', {
      Type: 'String',
    });
  });

  test('does not create SSM parameter when naming is not provided', () => {
    const stack = makeStack();

    new LambdaLayer(stack, 'NoSsm', {
      layerName: 'shared-d-use1-no-ssm-layer',
      code: lambda.Code.fromAsset(__dirname),
    });

    const template = Template.fromStack(stack);
    const ssmParams = template.findResources('AWS::SSM::Parameter');
    expect(Object.keys(ssmParams)).toHaveLength(0);
  });

  test('throws when neither code nor s3Bucket is provided', () => {
    const stack = makeStack();

    expect(() => {
      new LambdaLayer(stack, 'NoCode', {
        layerName: 'shared-d-use1-bad-layer',
      });
    }).toThrow(/either 'code' or 's3Bucket'/);
  });

  test('throws when s3Bucket is provided without s3Key', () => {
    const stack = makeStack();

    expect(() => {
      new LambdaLayer(stack, 'NoKey', {
        layerName: 'shared-d-use1-bad-layer',
        s3Bucket: 'my-bucket',
      });
    }).toThrow(/'s3Key' is required/);
  });

  test('creates layer from S3 bucket and key', () => {
    const stack = makeStack();

    new LambdaLayer(stack, 'S3Layer', {
      layerName: 'shared-d-use1-s3-layer',
      s3Bucket: 'my-deployment-bucket',
      s3Key: 'layers/shared-deps.zip',
    });

    const template = Template.fromStack(stack);

    template.hasResourceProperties('AWS::Lambda::LayerVersion', {
      LayerName: 'shared-d-use1-s3-layer',
      Content: {
        S3Bucket: 'my-deployment-bucket',
        S3Key: 'layers/shared-deps.zip',
      },
    });
  });

  test('sets license info when provided', () => {
    const stack = makeStack();

    new LambdaLayer(stack, 'Licensed', {
      layerName: 'shared-d-use1-licensed-layer',
      code: lambda.Code.fromAsset(__dirname),
      license: 'MIT',
    });

    const template = Template.fromStack(stack);

    template.hasResourceProperties('AWS::Lambda::LayerVersion', {
      LicenseInfo: 'MIT',
    });
  });

  test('exposes layerVersion and layerVersionArn', () => {
    const stack = makeStack();

    const layer = new LambdaLayer(stack, 'Exposed', {
      layerName: 'shared-d-use1-exposed-layer',
      code: lambda.Code.fromAsset(__dirname),
    });

    expect(layer.layerVersion).toBeDefined();
    expect(layer.layerVersionArn).toBeDefined();
  });

  test('generates default description when not provided', () => {
    const stack = makeStack();

    new LambdaLayer(stack, 'DefaultDesc', {
      layerName: 'shared-d-use1-auto-desc',
      code: lambda.Code.fromAsset(__dirname),
    });

    const template = Template.fromStack(stack);

    template.hasResourceProperties('AWS::Lambda::LayerVersion', {
      Description: 'Lambda layer: shared-d-use1-auto-desc',
    });
  });
});
