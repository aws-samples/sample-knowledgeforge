import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import { Template } from 'aws-cdk-lib/assertions';
import { StandardLambda } from '../standard-lambda';
import { NamingUtil } from '../../../utils/naming';

describe('StandardLambda', () => {
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

  test('creates Lambda function with correct properties', () => {
    new StandardLambda(stack, 'TestLambda', {
      naming,
      functionName: 'test-d-use1-test-function',
      runtime: cdk.aws_lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      code: cdk.aws_lambda.Code.fromInline('def handler(event, context): return {}'),
    });

    const template = Template.fromStack(stack);

    template.hasResourceProperties('AWS::Lambda::Function', {
      Runtime: 'python3.12',
      Handler: 'index.handler',
    });
  });

  test('creates SSM parameter for function ARN', () => {
    new StandardLambda(stack, 'TestLambda', {
      naming,
      functionName: 'test-d-use1-test-function',
      runtime: cdk.aws_lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      code: cdk.aws_lambda.Code.fromInline('def handler(event, context): return {}'),
    });

    const template = Template.fromStack(stack);

    template.hasResourceProperties('AWS::SSM::Parameter', {
      Type: 'String',
    });
  });

  test('applies environment variables correctly', () => {
    new StandardLambda(stack, 'TestLambda', {
      naming,
      functionName: 'test-d-use1-test-function',
      runtime: cdk.aws_lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      code: cdk.aws_lambda.Code.fromInline('def handler(event, context): return {}'),
      environment: {
        TEST_VAR: 'test-value',
      },
    });

    const template = Template.fromStack(stack);

    template.hasResourceProperties('AWS::Lambda::Function', {
      Environment: {
        Variables: {
          TEST_VAR: 'test-value',
        },
      },
    });
  });

  test('sets timeout and memory size', () => {
    new StandardLambda(stack, 'TestLambda', {
      naming,
      functionName: 'test-d-use1-test-function',
      runtime: cdk.aws_lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      code: cdk.aws_lambda.Code.fromInline('def handler(event, context): return {}'),
      timeout: cdk.Duration.seconds(60),
      memorySize: 1024,
    });

    const template = Template.fromStack(stack);

    template.hasResourceProperties('AWS::Lambda::Function', {
      Timeout: 60,
      MemorySize: 1024,
    });
  });
});

describe('StandardLambda VPC', () => {
  let app: cdk.App;
  let stack: cdk.Stack;

  beforeEach(() => {
    app = new cdk.App();
    stack = new cdk.Stack(app, 'TestStack');
  });

  test('creates Lambda in VPC with subnets and security groups', () => {
    const vpc = new ec2.Vpc(stack, 'TestVpc');
    const sg = new ec2.SecurityGroup(stack, 'TestSg', { vpc });

    new StandardLambda(stack, 'VpcLambda', {
      functionName: 'test-d-use1-vpc-function',
      runtime: cdk.aws_lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      code: cdk.aws_lambda.Code.fromInline('def handler(event, context): return {}'),
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

  test('creates Lambda without VPC when vpc prop is omitted', () => {
    new StandardLambda(stack, 'NoVpcLambda', {
      functionName: 'test-d-use1-novpc-function',
      runtime: cdk.aws_lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      code: cdk.aws_lambda.Code.fromInline('def handler(event, context): return {}'),
    });

    const template = Template.fromStack(stack);
    const functions = template.findResources('AWS::Lambda::Function');
    const fnKey = Object.keys(functions)[0];
    expect(functions[fnKey].Properties.VpcConfig).toBeUndefined();
  });
});
