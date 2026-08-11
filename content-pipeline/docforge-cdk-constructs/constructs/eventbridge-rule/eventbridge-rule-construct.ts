import * as cdk from 'aws-cdk-lib';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as sfn from 'aws-cdk-lib/aws-stepfunctions';
import { Construct } from 'constructs';

/**
 * Supported EventBridge rule target types.
 */
export type EventBridgeTargetType = 'lambda' | 'sqs' | 'sns' | 'stepfunction';

/**
 * Target configuration for an EventBridge rule.
 * Provide exactly one of the resource fields matching the `type`.
 */
export interface EventBridgeTarget {
  /** Target type */
  type: EventBridgeTargetType;
  /** Lambda function — required when type is 'lambda' */
  lambdaFunction?: lambda.IFunction;
  /** SQS queue — required when type is 'sqs' */
  queue?: sqs.IQueue;
  /** SNS topic — required when type is 'sns' */
  topic?: sns.ITopic;
  /** Step Functions state machine — required when type is 'stepfunction' */
  stateMachine?: sfn.IStateMachine;
}

/**
 * Props for EventBridgeRuleConstruct
 */
export interface EventBridgeRuleConstructProps {
  /** Name of the EventBridge rule */
  ruleName: string;
  /** Optional description */
  description?: string;
  /** Event pattern (mutually exclusive with schedule) */
  eventPattern?: events.EventPattern;
  /** Schedule expression (mutually exclusive with eventPattern) */
  schedule?: events.Schedule;
  /** Whether the rule is enabled (default: true) */
  enabled?: boolean;
  /** Optional custom event bus. Omit to use the default bus. */
  eventBus?: events.IEventBus;
  /** Rule targets */
  targets?: EventBridgeTarget[];
  /** Optional tags to apply */
  tags?: Record<string, string>;
}

/**
 * Reusable EventBridge Rule construct.
 *
 * Wraps aws-cdk-lib/aws-events Rule so consuming stacks
 * never import events directly.
 *
 * Supports schedule-based and event-pattern-based rules with
 * Lambda, SQS, SNS, and Step Functions targets.
 */
export class EventBridgeRuleConstruct extends Construct {
  public readonly rule: events.Rule;
  public readonly ruleArn: string;
  public readonly ruleName: string;

  constructor(scope: Construct, id: string, props: EventBridgeRuleConstructProps) {
    super(scope, id);

    if (!props.eventPattern && !props.schedule) {
      throw new Error('Either eventPattern or schedule must be provided');
    }
    if (props.eventPattern && props.schedule) {
      throw new Error('Cannot specify both eventPattern and schedule');
    }

    const rule = new events.Rule(this, 'Rule', {
      ruleName: props.ruleName,
      description: props.description,
      eventPattern: props.eventPattern,
      schedule: props.schedule,
      enabled: props.enabled ?? true,
      eventBus: props.eventBus,
    });

    if (props.targets) {
      for (const t of props.targets) {
        rule.addTarget(this.resolveTarget(t));
      }
    }

    if (props.tags) {
      Object.entries(props.tags).forEach(([k, v]) => {
        cdk.Tags.of(this).add(k, v);
      });
    }

    this.rule = rule;
    this.ruleArn = rule.ruleArn;
    this.ruleName = rule.ruleName;
  }

  private resolveTarget(target: EventBridgeTarget): events.IRuleTarget {
    switch (target.type) {
      case 'lambda':
        if (!target.lambdaFunction) throw new Error('lambdaFunction is required for lambda target');
        return new targets.LambdaFunction(target.lambdaFunction);
      case 'sqs':
        if (!target.queue) throw new Error('queue is required for sqs target');
        return new targets.SqsQueue(target.queue);
      case 'sns':
        if (!target.topic) throw new Error('topic is required for sns target');
        return new targets.SnsTopic(target.topic);
      case 'stepfunction':
        if (!target.stateMachine)
          throw new Error('stateMachine is required for stepfunction target');
        return new targets.SfnStateMachine(target.stateMachine);
      default:
        throw new Error(`Unsupported target type: ${target.type}`);
    }
  }
}
