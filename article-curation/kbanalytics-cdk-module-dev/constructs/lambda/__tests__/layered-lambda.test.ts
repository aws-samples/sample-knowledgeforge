import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import { Template } from 'aws-cdk-lib/assertions';
import { LayeredLambda } from '../layered-lambda';
import { NamingUtil } from '../../../utils/naming';

describe('LayeredLambda', () => {
  let app: cdk.App;
  let stack: cdk.Stack;
  let naming: NamingUtil;

  beforeEach(() => {
    app = new cdk.App();
    stack = new cdk.Stack(app, 'TestStack');
    naming = new NamingUtil({
      tenantId: 'test-tenant',
      envCode: 'd',
      regionCode: 'use1',
    });
  });

  test('creates Lambda function with layer', () => {
    new LayeredLambda(stack, 'TestLayeredLambda', {
      naming,
      functionName: 'test-d-use1-layered-lambda',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      code: lambda.Code.fromInline('def handler(event, context): return {}'),
      layerCode: lambda.Code.fromAsset(__dirname + '/test-layer'),
    });

    const template = Template.fromStack(stack);

    template.hasResourceProperties('AWS::Lambda::Function', {
      Runtime: 'python3.12',
      Handler: 'index.handler',
    });

    template.resourceCountIs('AWS::Lambda::LayerVersion', 1);
  });

  test('creates SSM parameter for function ARN', () => {
    new LayeredLambda(stack, 'TestLayeredLambda', {
      naming,
      functionName: 'test-d-use1-layered-lambda',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      code: lambda.Code.fromInline('def handler(event, context): return {}'),
      layerCode: lambda.Code.fromAsset(__dirname + '/test-layer'),
    });

    const template = Template.fromStack(stack);

    template.resourceCountIs('AWS::SSM::Parameter', 1);
  });

  test('applies environment variables correctly', () => {
    new LayeredLambda(stack, 'TestLayeredLambda', {
      naming,
      functionName: 'test-d-use1-layered-lambda',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      code: lambda.Code.fromInline('def handler(event, context): return {}'),
      layerCode: lambda.Code.fromAsset(__dirname + '/test-layer'),
      environment: {
        LAYER_VAR: 'layer-value',
      },
    });

    const template = Template.fromStack(stack);

    template.hasResourceProperties('AWS::Lambda::Function', {
      Environment: {
        Variables: {
          LAYER_VAR: 'layer-value',
        },
      },
    });
  });

  test('sets timeout and memory size', () => {
    new LayeredLambda(stack, 'TestLayeredLambda', {
      naming,
      functionName: 'test-d-use1-layered-lambda',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      code: lambda.Code.fromInline('def handler(event, context): return {}'),
      layerCode: lambda.Code.fromAsset(__dirname + '/test-layer'),
      timeout: cdk.Duration.seconds(90),
      memorySize: 512,
    });

    const template = Template.fromStack(stack);

    template.hasResourceProperties('AWS::Lambda::Function', {
      Timeout: 90,
      MemorySize: 512,
    });
  });

  test('applies custom layer description', () => {
    new LayeredLambda(stack, 'TestLayeredLambda', {
      naming,
      functionName: 'test-d-use1-layered-lambda',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      code: lambda.Code.fromInline('def handler(event, context): return {}'),
      layerCode: lambda.Code.fromAsset(__dirname + '/test-layer'),
      layerDescription: 'Custom layer description',
    });

    const template = Template.fromStack(stack);

    template.hasResourceProperties('AWS::Lambda::LayerVersion', {
      Description: 'Custom layer description',
    });
  });

  test('exposes Lambda function and layer properties', () => {
    const layeredLambda = new LayeredLambda(stack, 'TestLayeredLambda', {
      naming,
      functionName: 'test-d-use1-layered-lambda',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      code: lambda.Code.fromInline('def handler(event, context): return {}'),
      layerCode: lambda.Code.fromAsset(__dirname + '/test-layer'),
    });

    expect(layeredLambda.function).toBeDefined();
    expect(layeredLambda.layer).toBeDefined();
    expect(layeredLambda.functionArn).toBeDefined();
  });

  test('supports different runtimes', () => {
    new LayeredLambda(stack, 'TestLayeredLambda', {
      naming,
      functionName: 'test-d-use1-layered-lambda',
      runtime: lambda.Runtime.NODEJS_20_X,
      handler: 'index.handler',
      code: lambda.Code.fromInline('exports.handler = async () => {}'),
      layerCode: lambda.Code.fromAsset(__dirname + '/test-layer'),
    });

    const template = Template.fromStack(stack);

    template.hasResourceProperties('AWS::Lambda::Function', {
      Runtime: 'nodejs20.x',
    });
  });

  test('layer is compatible with function runtime', () => {
    new LayeredLambda(stack, 'TestLayeredLambda', {
      naming,
      functionName: 'test-d-use1-layered-lambda',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      code: lambda.Code.fromInline('def handler(event, context): return {}'),
      layerCode: lambda.Code.fromAsset(__dirname + '/test-layer'),
    });

    const template = Template.fromStack(stack);

    template.hasResourceProperties('AWS::Lambda::LayerVersion', {
      CompatibleRuntimes: ['python3.12'],
    });
  });
});

describe('LayeredLambda VPC', () => {
  let app: cdk.App;
  let stack: cdk.Stack;

  beforeEach(() => {
    app = new cdk.App();
    stack = new cdk.Stack(app, 'TestStack');
  });

  test('creates Layered Lambda in VPC with security groups', () => {
    const vpc = new ec2.Vpc(stack, 'TestVpc');
    const sg = new ec2.SecurityGroup(stack, 'TestSg', { vpc });

    new LayeredLambda(stack, 'VpcLayeredLambda', {
      functionName: 'test-d-use1-vpc-layered',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      code: lambda.Code.fromInline('def handler(event, context): return {}'),
      layerCode: lambda.Code.fromAsset(__dirname + '/test-layer'),
      vpc,
      vpcSubnets: { subnets: vpc.privateSubnets },
      securityGroups: [sg],
    });

    const template = Template.fromStack(stack);
    template.hasResourceProperties('AWS::Lambda::Function', {
      VpcConfig: {
        SecurityGroupIds: [
          { 'Fn::GetAtt': [stack.getLogicalId(sg.node.defaultChild as cdk.CfnElement), 'GroupId'] },
        ],
      },
    });
  });

  test('creates Layered Lambda without VPC when vpc prop is omitted', () => {
    new LayeredLambda(stack, 'NoVpcLayeredLambda', {
      functionName: 'test-d-use1-novpc-layered',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      code: lambda.Code.fromInline('def handler(event, context): return {}'),
      layerCode: lambda.Code.fromAsset(__dirname + '/test-layer'),
    });

    const template = Template.fromStack(stack);
    const functions = template.findResources('AWS::Lambda::Function');
    const fnKey = Object.keys(functions)[0];
    expect(functions[fnKey].Properties.VpcConfig).toBeUndefined();
  });
});
