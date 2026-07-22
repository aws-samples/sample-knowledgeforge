import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import { Template } from 'aws-cdk-lib/assertions';
import { DockerLambda } from '../docker-lambda';
import { NamingUtil } from '../../../utils/naming';

describe('DockerLambda', () => {
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

  test('creates Docker Lambda function with correct properties', () => {
    new DockerLambda(stack, 'TestDockerLambda', {
      naming,
      functionName: 'test-d-use1-docker-lambda',
      code: lambda.DockerImageCode.fromImageAsset(__dirname),
    });

    const template = Template.fromStack(stack);

    template.hasResourceProperties('AWS::Lambda::Function', {
      PackageType: 'Image',
    });
  });

  test('creates SSM parameter for function ARN', () => {
    new DockerLambda(stack, 'TestDockerLambda', {
      naming,
      functionName: 'test-d-use1-docker-lambda',
      code: lambda.DockerImageCode.fromImageAsset(__dirname),
    });

    const template = Template.fromStack(stack);

    template.resourceCountIs('AWS::SSM::Parameter', 1);
  });

  test('applies environment variables correctly', () => {
    new DockerLambda(stack, 'TestDockerLambda', {
      naming,
      functionName: 'test-d-use1-docker-lambda',
      code: lambda.DockerImageCode.fromImageAsset(__dirname),
      environment: {
        TEST_VAR: 'test-value',
        ANOTHER_VAR: 'another-value',
      },
    });

    const template = Template.fromStack(stack);

    template.hasResourceProperties('AWS::Lambda::Function', {
      Environment: {
        Variables: {
          TEST_VAR: 'test-value',
          ANOTHER_VAR: 'another-value',
        },
      },
    });
  });

  test('sets timeout and memory size', () => {
    new DockerLambda(stack, 'TestDockerLambda', {
      naming,
      functionName: 'test-d-use1-docker-lambda',
      code: lambda.DockerImageCode.fromImageAsset(__dirname),
      timeout: cdk.Duration.seconds(120),
      memorySize: 2048,
    });

    const template = Template.fromStack(stack);

    template.hasResourceProperties('AWS::Lambda::Function', {
      Timeout: 120,
      MemorySize: 2048,
    });
  });

  test('exposes Lambda function properties', () => {
    const dockerLambda = new DockerLambda(stack, 'TestDockerLambda', {
      naming,
      functionName: 'test-d-use1-docker-lambda',
      code: lambda.DockerImageCode.fromImageAsset(__dirname),
    });

    expect(dockerLambda.function).toBeDefined();
    expect(dockerLambda.functionArn).toBeDefined();
  });

  test('creates execution role with correct permissions', () => {
    new DockerLambda(stack, 'TestDockerLambda', {
      naming,
      functionName: 'test-d-use1-docker-lambda',
      code: lambda.DockerImageCode.fromImageAsset(__dirname),
    });

    const template = Template.fromStack(stack);

    template.hasResourceProperties('AWS::IAM::Role', {
      AssumeRolePolicyDocument: {
        Statement: [
          {
            Action: 'sts:AssumeRole',
            Effect: 'Allow',
            Principal: {
              Service: 'lambda.amazonaws.com',
            },
          },
        ],
      },
    });
  });
});

describe('DockerLambda VPC', () => {
  let app: cdk.App;
  let stack: cdk.Stack;

  beforeEach(() => {
    app = new cdk.App();
    stack = new cdk.Stack(app, 'TestStack');
  });

  test('creates Docker Lambda in VPC with security groups', () => {
    const vpc = new ec2.Vpc(stack, 'TestVpc');
    const sg = new ec2.SecurityGroup(stack, 'TestSg', { vpc });

    new DockerLambda(stack, 'VpcDockerLambda', {
      functionName: 'test-d-use1-vpc-docker',
      code: lambda.DockerImageCode.fromImageAsset(__dirname),
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

  test('creates Docker Lambda without VPC when vpc prop is omitted', () => {
    new DockerLambda(stack, 'NoVpcDockerLambda', {
      functionName: 'test-d-use1-novpc-docker',
      code: lambda.DockerImageCode.fromImageAsset(__dirname),
    });

    const template = Template.fromStack(stack);
    const functions = template.findResources('AWS::Lambda::Function');
    const fnKey = Object.keys(functions)[0];
    expect(functions[fnKey].Properties.VpcConfig).toBeUndefined();
  });
});
