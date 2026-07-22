import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { LlmOpsConstruct } from '../llmops-construct';

describe('LlmOpsConstruct', () => {
  let app: cdk.App;
  let stack: cdk.Stack;

  beforeEach(() => {
    app = new cdk.App();
    stack = new cdk.Stack(app, 'TestStack');
  });

  test('creates invocation log group, S3 bucket, logging role, and dashboard', () => {
    new LlmOpsConstruct(stack, 'TestLlmOps', {
      resourcePrefix: 'test-d-use1',
    });

    const template = Template.fromStack(stack);
    template.resourceCountIs('AWS::Logs::LogGroup', 1);
    template.resourceCountIs('AWS::S3::Bucket', 1);
    template.resourceCountIs('AWS::IAM::Role', 2); // logging role + config Lambda role
    template.resourceCountIs('AWS::CloudWatch::Dashboard', 1);
  });

  test('creates 4 CloudWatch alarms', () => {
    new LlmOpsConstruct(stack, 'TestLlmOps', {
      resourcePrefix: 'test-d-use1',
      alarms: {
        throttleThreshold: 5,
        latencyThresholdMs: 20000,
        errorThreshold: 3,
        guardrailInterventionThreshold: 3,
      },
    });

    const template = Template.fromStack(stack);
    template.resourceCountIs('AWS::CloudWatch::Alarm', 4);
  });

  test('creates SSM parameters when ssmPrefix provided', () => {
    new LlmOpsConstruct(stack, 'TestLlmOps', {
      resourcePrefix: 'test-d-use1',
      ssmPrefix: '/test-d-use1',
    });

    const template = Template.fromStack(stack);
    template.resourceCountIs('AWS::SSM::Parameter', 3);
  });

  test('exposes log group, bucket, and dashboard', () => {
    const llmops = new LlmOpsConstruct(stack, 'TestLlmOps', {
      resourcePrefix: 'test-d-use1',
    });

    expect(llmops.invocationLogGroup).toBeDefined();
    expect(llmops.invocationLogBucket).toBeDefined();
    expect(llmops.dashboard).toBeDefined();
    expect(llmops.loggingRole).toBeDefined();
  });
});
