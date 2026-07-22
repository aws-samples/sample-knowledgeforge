# EventBridgeRuleConstruct

Reusable Amazon EventBridge Rule construct. Supports schedule-based and event-pattern-based rules with Lambda, SQS, SNS, and Step Functions targets.

## Usage

```typescript
import { EventBridgeRuleConstruct } from '@kbanalytics/cdk-constructs';

// Event-pattern rule with Lambda target
const rule = new EventBridgeRuleConstruct(this, 'OrderRule', {
  ruleName: 'acme-d-use1-order-created',
  description: 'Triggers on new order events',
  eventPattern: {
    source: ['com.acme.orders'],
    detailType: ['OrderCreated'],
  },
  targets: [
    { type: 'lambda', lambdaFunction: myLambda },
  ],
  tags: { Environment: 'dev' },
});

// Schedule-based rule with SQS target
const scheduled = new EventBridgeRuleConstruct(this, 'CronRule', {
  ruleName: 'acme-d-use1-daily-sync',
  schedule: events.Schedule.cron({ hour: '6', minute: '0' }),
  targets: [
    { type: 'sqs', queue: myQueue },
  ],
});
```

## Types

### `EventBridgeTargetType`

```typescript
type EventBridgeTargetType = 'lambda' | 'sqs' | 'sns' | 'stepfunction';
```

### `EventBridgeTarget`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| type | `EventBridgeTargetType` | Yes | Target type |
| lambdaFunction | `lambda.IFunction` | When type is `'lambda'` | Lambda function target |
| queue | `sqs.IQueue` | When type is `'sqs'` | SQS queue target |
| topic | `sns.ITopic` | When type is `'sns'` | SNS topic target |
| stateMachine | `sfn.IStateMachine` | When type is `'stepfunction'` | Step Functions state machine target |

## Props

### `EventBridgeRuleConstructProps`

| Prop | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| ruleName | `string` | Yes | - | Name of the EventBridge rule |
| description | `string` | No | - | Rule description |
| eventPattern | `events.EventPattern` | No | - | Event pattern (mutually exclusive with `schedule`) |
| schedule | `events.Schedule` | No | - | Schedule expression (mutually exclusive with `eventPattern`) |
| enabled | `boolean` | No | `true` | Whether the rule is enabled |
| eventBus | `events.IEventBus` | No | Default bus | Custom event bus |
| targets | `EventBridgeTarget[]` | No | - | Rule targets |
| tags | `Record<string, string>` | No | - | Tags to apply |

## Properties (read-only)

| Property | Type | Description |
|----------|------|-------------|
| rule | `events.Rule` | The underlying EventBridge Rule |
| ruleArn | `string` | Rule ARN |
| ruleName | `string` | Rule name |

## Config-Driven Usage

```yaml
eventbridge_rules:
  order-created:
    rule_name: "acme-d-use1-order-created"
    description: "Triggers on new order events"
    event_pattern:
      source: ["com.acme.orders"]
      detail_type: ["OrderCreated"]
    targets:
      - type: lambda
        function_arn: "arn:aws:lambda:us-east-1:123456789012:function:order-handler"
    tags:
      Environment: dev
```

## Notes

- Exactly one of `eventPattern` or `schedule` must be provided; specifying both throws an error.
- Provide exactly one resource field per target matching the `type` (e.g. `lambdaFunction` for `'lambda'`).
- The construct wraps `aws-cdk-lib/aws-events.Rule` so consuming stacks never import `events` directly.
