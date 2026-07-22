import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as sfn from 'aws-cdk-lib/aws-stepfunctions';
import * as events from 'aws-cdk-lib/aws-events';
import { EventBridgeRuleConstruct } from '../eventbridge-rule-construct';

function makeStack() {
  const app = new cdk.App();
  return new cdk.Stack(app, 'TestStack');
}

describe('EventBridgeRuleConstruct', () => {
  test('creates a rule with an event pattern', () => {
    const stack = makeStack();

    new EventBridgeRuleConstruct(stack, 'TestRule', {
      ruleName: 'kbanalytics-d-use1-order-created',
      description: 'Fires on order creation',
      eventPattern: {
        source: ['com.example.orders'],
        detailType: ['OrderCreated'],
      },
    });

    const template = Template.fromStack(stack);
    template.hasResourceProperties('AWS::Events::Rule', {
      Name: 'kbanalytics-d-use1-order-created',
      Description: 'Fires on order creation',
      EventPattern: {
        source: ['com.example.orders'],
        'detail-type': ['OrderCreated'],
      },
      State: 'ENABLED',
    });
  });

  test('creates a schedule-based rule', () => {
    const stack = makeStack();

    new EventBridgeRuleConstruct(stack, 'TestRule', {
      ruleName: 'kbanalytics-d-use1-daily-sync',
      schedule: events.Schedule.rate(cdk.Duration.hours(1)),
    });

    const template = Template.fromStack(stack);
    template.hasResourceProperties('AWS::Events::Rule', {
      Name: 'kbanalytics-d-use1-daily-sync',
      ScheduleExpression: 'rate(1 hour)',
      State: 'ENABLED',
    });
  });

  test('creates a disabled rule when enabled is false', () => {
    const stack = makeStack();

    new EventBridgeRuleConstruct(stack, 'TestRule', {
      ruleName: 'kbanalytics-d-use1-disabled-rule',
      eventPattern: { source: ['com.example.test'] },
      enabled: false,
    });

    const template = Template.fromStack(stack);
    template.hasResourceProperties('AWS::Events::Rule', {
      Name: 'kbanalytics-d-use1-disabled-rule',
      State: 'DISABLED',
    });
  });

  test('adds a Lambda target', () => {
    const stack = makeStack();
    const fn = new lambda.Function(stack, 'Fn', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      code: lambda.Code.fromInline('def handler(e,c): pass'),
    });

    new EventBridgeRuleConstruct(stack, 'TestRule', {
      ruleName: 'kbanalytics-d-use1-lambda-target',
      eventPattern: { source: ['com.example.test'] },
      targets: [{ type: 'lambda', lambdaFunction: fn }],
    });

    const template = Template.fromStack(stack);
    template.hasResourceProperties('AWS::Events::Rule', {
      Targets: Match.arrayWith([
        Match.objectLike({
          Arn: { 'Fn::GetAtt': Match.anyValue() },
        }),
      ]),
    });
  });

  test('adds an SQS target', () => {
    const stack = makeStack();
    const queue = new sqs.Queue(stack, 'Queue');

    new EventBridgeRuleConstruct(stack, 'TestRule', {
      ruleName: 'kbanalytics-d-use1-sqs-target',
      eventPattern: { source: ['com.example.test'] },
      targets: [{ type: 'sqs', queue }],
    });

    const template = Template.fromStack(stack);
    template.hasResourceProperties('AWS::Events::Rule', {
      Targets: Match.arrayWith([
        Match.objectLike({
          Arn: { 'Fn::GetAtt': Match.anyValue() },
        }),
      ]),
    });
  });

  test('adds an SNS target', () => {
    const stack = makeStack();
    const topic = new sns.Topic(stack, 'Topic');

    new EventBridgeRuleConstruct(stack, 'TestRule', {
      ruleName: 'kbanalytics-d-use1-sns-target',
      eventPattern: { source: ['com.example.test'] },
      targets: [{ type: 'sns', topic }],
    });

    const template = Template.fromStack(stack);
    template.hasResourceProperties('AWS::Events::Rule', {
      Targets: Match.arrayWith([
        Match.objectLike({
          Arn: Match.anyValue(),
        }),
      ]),
    });
  });

  test('adds a Step Functions target', () => {
    const stack = makeStack();
    const sm = new sfn.StateMachine(stack, 'SM', {
      definitionBody: sfn.DefinitionBody.fromChainable(new sfn.Pass(stack, 'Start')),
    });

    new EventBridgeRuleConstruct(stack, 'TestRule', {
      ruleName: 'kbanalytics-d-use1-sfn-target',
      eventPattern: { source: ['com.example.test'] },
      targets: [{ type: 'stepfunction', stateMachine: sm }],
    });

    const template = Template.fromStack(stack);
    template.hasResourceProperties('AWS::Events::Rule', {
      Targets: Match.arrayWith([
        Match.objectLike({
          Arn: Match.anyValue(),
          RoleArn: Match.anyValue(),
        }),
      ]),
    });
  });

  test('applies tags', () => {
    const stack = makeStack();

    new EventBridgeRuleConstruct(stack, 'TestRule', {
      ruleName: 'kbanalytics-d-use1-tagged-rule',
      eventPattern: { source: ['com.example.test'] },
      tags: { Environment: 'dev', Project: 'KBAnalytics' },
    });

    const template = Template.fromStack(stack);
    template.hasResourceProperties('AWS::Events::Rule', {
      Tags: Match.arrayWith([
        { Key: 'Environment', Value: 'dev' },
        { Key: 'Project', Value: 'KBAnalytics' },
      ]),
    });
  });

  test('throws when neither eventPattern nor schedule is provided', () => {
    const stack = makeStack();

    expect(() => {
      new EventBridgeRuleConstruct(stack, 'TestRule', {
        ruleName: 'kbanalytics-d-use1-bad-rule',
      });
    }).toThrow('Either eventPattern or schedule must be provided');
  });

  test('throws when both eventPattern and schedule are provided', () => {
    const stack = makeStack();

    expect(() => {
      new EventBridgeRuleConstruct(stack, 'TestRule', {
        ruleName: 'kbanalytics-d-use1-bad-rule',
        eventPattern: { source: ['com.example.test'] },
        schedule: events.Schedule.rate(cdk.Duration.hours(1)),
      });
    }).toThrow('Cannot specify both eventPattern and schedule');
  });

  test('throws when lambda target is missing lambdaFunction', () => {
    const stack = makeStack();

    expect(() => {
      new EventBridgeRuleConstruct(stack, 'TestRule', {
        ruleName: 'kbanalytics-d-use1-bad-target',
        eventPattern: { source: ['com.example.test'] },
        targets: [{ type: 'lambda' }],
      });
    }).toThrow('lambdaFunction is required for lambda target');
  });

  test('throws when sqs target is missing queue', () => {
    const stack = makeStack();

    expect(() => {
      new EventBridgeRuleConstruct(stack, 'TestRule', {
        ruleName: 'kbanalytics-d-use1-bad-target',
        eventPattern: { source: ['com.example.test'] },
        targets: [{ type: 'sqs' }],
      });
    }).toThrow('queue is required for sqs target');
  });

  test('exposes rule, ruleArn, and ruleName as public readonly', () => {
    const stack = makeStack();

    const construct = new EventBridgeRuleConstruct(stack, 'TestRule', {
      ruleName: 'kbanalytics-d-use1-exposed-rule',
      eventPattern: { source: ['com.example.test'] },
    });

    expect(construct.rule).toBeDefined();
    expect(construct.ruleArn).toBeDefined();
    expect(construct.ruleName).toBeDefined();
  });

  test('supports multiple targets', () => {
    const stack = makeStack();
    const fn = new lambda.Function(stack, 'Fn', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      code: lambda.Code.fromInline('def handler(e,c): pass'),
    });
    const queue = new sqs.Queue(stack, 'Queue');

    new EventBridgeRuleConstruct(stack, 'TestRule', {
      ruleName: 'kbanalytics-d-use1-multi-target',
      eventPattern: { source: ['com.example.test'] },
      targets: [
        { type: 'lambda', lambdaFunction: fn },
        { type: 'sqs', queue },
      ],
    });

    const template = Template.fromStack(stack);
    template.hasResourceProperties('AWS::Events::Rule', {
      Targets: Match.arrayWith([
        Match.objectLike({ Arn: Match.anyValue() }),
        Match.objectLike({ Arn: Match.anyValue() }),
      ]),
    });
  });

  test('snapshot', () => {
    const stack = makeStack();

    new EventBridgeRuleConstruct(stack, 'TestRule', {
      ruleName: 'kbanalytics-d-use1-snapshot-rule',
      description: 'Snapshot test rule',
      eventPattern: {
        source: ['com.example.orders'],
        detailType: ['OrderCreated'],
      },
      tags: { Environment: 'dev' },
    });

    const template = Template.fromStack(stack);
    expect(template.toJSON()).toMatchSnapshot();
  });
});
