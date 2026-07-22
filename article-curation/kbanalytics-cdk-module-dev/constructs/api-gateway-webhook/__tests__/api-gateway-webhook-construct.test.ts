import * as cdk from 'aws-cdk-lib';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import { Template } from 'aws-cdk-lib/assertions';
import { ApiGatewayWebhookConstruct } from '../api-gateway-webhook-construct';
import { NamingUtil } from '../../../utils/naming';

describe('ApiGatewayWebhookConstruct', () => {
  let app: cdk.App;
  let stack: cdk.Stack;
  let naming: NamingUtil;
  let handler: lambda.Function;

  beforeEach(() => {
    app = new cdk.App();
    stack = new cdk.Stack(app, 'TestStack');
    naming = new NamingUtil({ tenantId: 'test', envCode: 'd', regionCode: 'use1' });
    handler = new lambda.Function(stack, 'TestHandler', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      code: lambda.Code.fromInline('def handler(e,c): return {"statusCode":200}'),
    });
  });

  test('creates REST API, Cognito User Pool, and authorizer', () => {
    new ApiGatewayWebhookConstruct(stack, 'TestWebhook', {
      apiName: 'test-d-use1-webhook',
      handler,
      pathSegments: ['webhook', 'review-callback'],
      naming,
    });

    const template = Template.fromStack(stack);
    template.resourceCountIs('AWS::ApiGateway::RestApi', 1);
    template.resourceCountIs('AWS::Cognito::UserPool', 1);
    template.resourceCountIs('AWS::Cognito::UserPoolClient', 1);
    template.resourceCountIs('AWS::Cognito::UserPoolResourceServer', 1);
    template.resourceCountIs('AWS::Cognito::UserPoolDomain', 1);
  });

  test('uses existing Cognito pool when ARN provided', () => {
    new ApiGatewayWebhookConstruct(stack, 'TestWebhook', {
      apiName: 'test-d-use1-webhook',
      handler,
      pathSegments: ['webhook'],
      cognitoUserPoolArn: 'arn:aws:cognito-idp:us-east-1:123456789012:userpool/us-east-1_abc123',
      naming,
    });

    const template = Template.fromStack(stack);
    template.resourceCountIs('AWS::ApiGateway::RestApi', 1);
    template.resourceCountIs('AWS::Cognito::UserPool', 0);
  });

  test('creates SSM parameter when ssmPrefix provided', () => {
    new ApiGatewayWebhookConstruct(stack, 'TestWebhook', {
      apiName: 'test-d-use1-webhook',
      handler,
      pathSegments: ['webhook'],
      naming,
      ssmPrefix: '/test-d-use1',
    });

    const template = Template.fromStack(stack);
    template.resourceCountIs('AWS::SSM::Parameter', 1);
  });

  test('exposes api, apiUrl, and userPool', () => {
    const webhook = new ApiGatewayWebhookConstruct(stack, 'TestWebhook', {
      apiName: 'test-d-use1-webhook',
      handler,
      pathSegments: ['webhook'],
    });

    expect(webhook.api).toBeDefined();
    expect(webhook.apiUrl).toBeDefined();
    expect(webhook.userPool).toBeDefined();
  });