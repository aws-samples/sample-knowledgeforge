import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { LambdaRoleConstruct } from './lambda-role';

describe('LambdaRoleConstruct', () => {
  let app: cdk.App;
  let stack: cdk.Stack;

  beforeEach(() => {
    app = new cdk.App();
    stack = new cdk.Stack(app, 'TestStack');
  });

  test('creates role with Lambda assume-role and basic execution policy', () => {
    new LambdaRoleConstruct(stack, 'TestRole', {
      roleName: 'test-lambda-role',
    });

    const template = Template.fromStack(stack);
    template.hasResourceProperties('AWS::IAM::Role', {
      RoleName: 'test-lambda-role',
      AssumeRolePolicyDocument: {
        Statement: [
          {
            Action: 'sts:AssumeRole',
            Effect: 'Allow',
            Principal: { Service: 'lambda.amazonaws.com' },
          },
        ],
      },
      ManagedPolicyArns: [
        {
          'Fn::Join': [
            '',
            [
              'arn:',
              { Ref: 'AWS::Partition' },
              ':iam::aws:policy/service-role/AWSLambdaBasicExecutionRole',
            ],
          ],
        },
      ],
    });
  });

  test('adds KMS decrypt policy with function name condition', () => {
    new LambdaRoleConstruct(stack, 'TestRole', {
      roleName: 'test-lambda-role',
      kmsDecryptKeyArn: 'arn:aws:kms:us-east-1:123456789012:key/test-key-id',
      functionName: 'my-lambda-function',
    });

    const template = Template.fromStack(stack);
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: [
          {
            Action: 'kms:Decrypt',
            Effect: 'Allow',
            Resource: 'arn:aws:kms:us-east-1:123456789012:key/test-key-id',
            Condition: {
              StringLike: {
                'kms:EncryptionContext:LambdaFunctionName': 'my-lambda-function',
              },
            },
          },
        ],
      },
    });
  });

  test('adds custom inline policies from config', () => {
    new LambdaRoleConstruct(stack, 'TestRole', {
      policies: [
        {
          actions: ['dynamodb:GetItem', 'dynamodb:PutItem'],
          resources: ['arn:aws:dynamodb:us-east-1:123456789012:table/my-table'],
        },
        {
          actions: ['s3:GetObject'],
          resources: ['arn:aws:s3:::my-bucket/*'],
        },
      ],
    });

    const template = Template.fromStack(stack);
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: [
          {
            Action: ['dynamodb:GetItem', 'dynamodb:PutItem'],
            Effect: 'Allow',
            Resource: 'arn:aws:dynamodb:us-east-1:123456789012:table/my-table',
          },
          {
            Action: 's3:GetObject',
            Effect: 'Allow',
            Resource: 'arn:aws:s3:::my-bucket/*',
          },
        ],
      },
    });
  });

  test('creates role without explicit name when omitted', () => {
    new LambdaRoleConstruct(stack, 'AutoNameRole', {});

    const template = Template.fromStack(stack);
    // Should have a role but without explicit RoleName
    const roles = template.findResources('AWS::IAM::Role');
    const roleKeys = Object.keys(roles);
    expect(roleKeys.length).toBe(1);
    expect(roles[roleKeys[0]].Properties.RoleName).toBeUndefined();
  });
});
