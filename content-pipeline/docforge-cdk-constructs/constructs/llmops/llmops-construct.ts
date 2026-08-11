import * as cdk from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

/**
 * LLMOps construct for LLM observability.
 *
 * Reusable across any Bedrock LLM workload. Creates:
 *   - Model invocation logging (CloudWatch + S3)
 *   - CloudWatch dashboard (invocations, tokens, latency, guardrails)
 *   - CloudWatch alarms with configurable thresholds
 *   - Helper method to grant Bedrock IAM permissions
 */
export interface LlmOpsAlarmConfig {
  throttleThreshold?: number;
  latencyThresholdMs?: number;
  errorThreshold?: number;
  guardrailInterventionThreshold?: number;
  snsTopicArn?: string;
}

export interface LlmOpsConstructProps {
  /** Resource name prefix (e.g. 'docforge-s-euw1') */
  resourcePrefix: string;
  /** Alarm configuration */
  alarms?: LlmOpsAlarmConfig;
  /** Log retention in days (default: 90) */
  logRetentionDays?: number;
  /** S3 log expiration in days (default: 90) */
  s3LogExpirationDays?: number;
  /** Optional SSM prefix for storing resource names */
  ssmPrefix?: string;
  /** Import existing bucket instead of creating new one */
  importExistingBucket?: boolean;
  /** Import existing log group instead of creating new one */
  importExistingLogGroup?: boolean;
  /** Skip Bedrock logging configuration (useful when importing existing resources) */
  skipLoggingConfig?: boolean;
}

export class LlmOpsConstruct extends Construct {
  public readonly invocationLogGroup: logs.ILogGroup;
  public readonly invocationLogBucket: s3.IBucket;
  public readonly loggingRole: iam.Role;
  public readonly dashboard: cloudwatch.Dashboard;

  constructor(scope: Construct, id: string, props: LlmOpsConstructProps) {
    super(scope, id);

    const stack = cdk.Stack.of(this);
    const region = stack.region;
    const prefix = props.resourcePrefix;
    const logRetention = props.logRetentionDays ?? 90;
    const s3Expiration = props.s3LogExpirationDays ?? 90;

    // Invocation logging
    this.invocationLogGroup = props.importExistingLogGroup
      ? logs.LogGroup.fromLogGroupName(this, 'InvocationLogs', `/aws/bedrock/${prefix}-llm_invocation_logs`)
      : new logs.LogGroup(this, 'InvocationLogs', {
          logGroupName: `/aws/bedrock/${prefix}-llm_invocation_logs`,
          retention: this.toRetentionDays(logRetention),
          removalPolicy: cdk.RemovalPolicy.RETAIN,
        });

    this.invocationLogBucket = props.importExistingBucket
      ? s3.Bucket.fromBucketName(this, 'InvocationLogBucket', `${prefix}-llm-invocation-log`)
      : new s3.Bucket(this, 'InvocationLogBucket', {
          bucketName: `${prefix}-llm-invocation-log`,
          removalPolicy: cdk.RemovalPolicy.RETAIN,
          lifecycleRules: [{ expiration: cdk.Duration.days(s3Expiration) }],
          encryption: s3.BucketEncryption.S3_MANAGED,
          blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
          enforceSSL: true,
        });

    this.loggingRole = new iam.Role(this, 'LoggingRole', {
      roleName: `${prefix}-bedrock_logging_role`,
      assumedBy: new iam.ServicePrincipal('bedrock.amazonaws.com'),
    });

    this.invocationLogGroup.grantWrite(this.loggingRole);
    this.invocationLogBucket.grantPut(this.loggingRole);

    // Bucket policy for Bedrock — skip when importing to avoid Access Denied
    if (!props.importExistingBucket) {
      this.invocationLogBucket.addToResourcePolicy(new iam.PolicyStatement({
        actions: ['s3:PutObject'],
        resources: [`${this.invocationLogBucket.bucketArn}/*`],
        principals: [new iam.ServicePrincipal('bedrock.amazonaws.com')],
      }));
    }

    // Bedrock logging config — skip when importing existing resources
    if (!props.skipLoggingConfig) {
      this.createLoggingConfig(prefix, region);
    }

    // Dashboard
    this.dashboard = this.createDashboard(prefix);

    // Alarms
    this.createAlarms(prefix, props.alarms ?? {});

    // SSM parameters
    if (props.ssmPrefix) {
      new ssm.StringParameter(this, 'LogGroupParam', {
        parameterName: `${props.ssmPrefix}/llmops/invocation-log-group`,
        stringValue: this.invocationLogGroup.logGroupName,
      });
      new ssm.StringParameter(this, 'LogBucketParam', {
        parameterName: `${props.ssmPrefix}/llmops/invocation-log-bucket`,
        stringValue: this.invocationLogBucket.bucketName,
      });
      new ssm.StringParameter(this, 'DashboardParam', {
        parameterName: `${props.ssmPrefix}/llmops/dashboard-name`,
        stringValue: this.dashboard.dashboardName,
      });
    }
  }

  /** Grant Bedrock invoke + prompt + guardrail permissions to a role */
  public grantBedrockInvoke(role: iam.IRole, modelArns: string[], guardrailArn?: string): void {
    const stack = cdk.Stack.of(this);
    role.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['bedrock:InvokeModel'],
      resources: modelArns,
    }));
    role.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['bedrock:GetPrompt', 'bedrock:RenderPrompt'],
      resources: [`arn:aws:bedrock:${stack.region}:${stack.account}:prompt/*`],
    }));
    if (guardrailArn) {
      role.addToPrincipalPolicy(new iam.PolicyStatement({
        actions: ['bedrock:ApplyGuardrail'],
        resources: [guardrailArn],
      }));
    }
  }

  private createLoggingConfig(prefix: string, region: string): void {
    const configFn = new lambda.Function(this, 'LoggingConfigFn', {
      functionName: `${prefix}-bedrock_logging_config`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      timeout: cdk.Duration.seconds(60),
      code: lambda.Code.fromInline(`
import boto3, json, traceback
import urllib.request
def send_response(event, context, status, data={}):
    body = json.dumps({'Status': status, 'Reason': f'See CloudWatch Log Stream: {context.log_stream_name}',
        'PhysicalResourceId': context.log_stream_name, 'StackId': event['StackId'],
        'RequestId': event['RequestId'], 'LogicalResourceId': event['LogicalResourceId'], 'Data': data})
    req = urllib.request.Request(event['ResponseURL'], data=body.encode('utf-8'),
        headers={'Content-Type': ''}, method='PUT')
    urllib.request.urlopen(req)
def handler(event, context):
    try:
        props = event['ResourceProperties']
        rt = event['RequestType']
        bedrock = boto3.client('bedrock', region_name=props['Region'])
        if rt in ('Create', 'Update'):
            bedrock.put_model_invocation_logging_configuration(loggingConfig={
                'cloudWatchConfig': {'logGroupName': props['LogGroupName'], 'roleArn': props['LoggingRoleArn'],
                    'largeDataDeliveryS3Config': {'bucketName': props['BucketName'], 'keyPrefix': 'large-data/'}},
                's3Config': {'bucketName': props['BucketName'], 'keyPrefix': 'invocation-logs/'},
                'textDataDeliveryEnabled': True, 'imageDataDeliveryEnabled': False, 'embeddingDataDeliveryEnabled': False})
        elif rt == 'Delete':
            bedrock.delete_model_invocation_logging_configuration()
        send_response(event, context, 'SUCCESS')
    except Exception as e:
        print(f'Error: {e}'); traceback.print_exc()
        send_response(event, context, 'FAILED', {'Error': str(e)})
`),
    });

    configFn.role!.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['bedrock:PutModelInvocationLoggingConfiguration', 'bedrock:GetModelInvocationLoggingConfiguration', 'bedrock:DeleteModelInvocationLoggingConfiguration'],
      resources: ['*'],
    }));
    configFn.role!.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['iam:PassRole'],
      resources: [this.loggingRole.roleArn],
    }));

    const loggingConfig = new cdk.CustomResource(this, 'LoggingConfig', {
      serviceToken: configFn.functionArn,
      properties: { Region: region, LogGroupName: this.invocationLogGroup.logGroupName, BucketName: this.invocationLogBucket.bucketName, LoggingRoleArn: this.loggingRole.roleArn },
    });
    // Ensure bucket policy and role are ready before Bedrock validates them
    loggingConfig.node.addDependency(this.invocationLogBucket);
    loggingConfig.node.addDependency(this.loggingRole);
  }

  private createDashboard(prefix: string): cloudwatch.Dashboard {
    const dashboard = new cloudwatch.Dashboard(this, 'Dashboard', { dashboardName: `${prefix}-llmops-dashboard` });
    const m = (ns: string, name: string, stat: string) => new cloudwatch.Metric({ namespace: ns, metricName: name, statistic: stat, period: cdk.Duration.minutes(5) });

    dashboard.addWidgets(
      new cloudwatch.GraphWidget({ title: 'Invocations', left: [m('AWS/Bedrock', 'Invocations', 'Sum')], width: 8, height: 6 }),
      new cloudwatch.GraphWidget({ title: 'Throttles & Errors', left: [m('AWS/Bedrock', 'InvocationThrottles', 'Sum'), m('AWS/Bedrock', 'InvocationClientErrors', 'Sum'), m('AWS/Bedrock', 'InvocationServerErrors', 'Sum')], width: 8, height: 6 }),
      new cloudwatch.SingleValueWidget({ title: 'Total Invocations (24h)', metrics: [m('AWS/Bedrock', 'Invocations', 'Sum').with({ period: cdk.Duration.hours(24) })], width: 8, height: 6 }),
    );
    dashboard.addWidgets(
      new cloudwatch.GraphWidget({ title: 'Input Tokens', left: [m('AWS/Bedrock', 'InputTokenCount', 'Sum')], width: 8, height: 6 }),
      new cloudwatch.GraphWidget({ title: 'Output Tokens', left: [m('AWS/Bedrock', 'OutputTokenCount', 'Sum')], width: 8, height: 6 }),
      new cloudwatch.GraphWidget({ title: 'Input vs Output', left: [m('AWS/Bedrock', 'InputTokenCount', 'Sum')], right: [m('AWS/Bedrock', 'OutputTokenCount', 'Sum')], width: 8, height: 6 }),
    );
    dashboard.addWidgets(
      new cloudwatch.GraphWidget({ title: 'Latency (Avg/P90/P99)', left: [m('AWS/Bedrock', 'InvocationLatency', 'Average'), m('AWS/Bedrock', 'InvocationLatency', 'p90'), m('AWS/Bedrock', 'InvocationLatency', 'p99')], width: 12, height: 6 }),
      new cloudwatch.SingleValueWidget({ title: 'Avg Latency (1h)', metrics: [m('AWS/Bedrock', 'InvocationLatency', 'Average').with({ period: cdk.Duration.hours(1) })], width: 6, height: 6 }),
      new cloudwatch.SingleValueWidget({ title: 'P90 Latency (1h)', metrics: [m('AWS/Bedrock', 'InvocationLatency', 'p90').with({ period: cdk.Duration.hours(1) })], width: 6, height: 6 }),
    );
    dashboard.addWidgets(
      new cloudwatch.GraphWidget({ title: 'Guardrail Invocations vs Interventions', left: [m('AWS/Bedrock/Guardrails', 'Invocations', 'Sum'), m('AWS/Bedrock/Guardrails', 'InvocationsIntervened', 'Sum')], width: 12, height: 6 }),
      new cloudwatch.GraphWidget({ title: 'Guardrail Latency', left: [m('AWS/Bedrock/Guardrails', 'InvocationLatency', 'Average')], width: 6, height: 6 }),
      new cloudwatch.SingleValueWidget({ title: 'Guardrail Blocks (24h)', metrics: [m('AWS/Bedrock/Guardrails', 'InvocationsIntervened', 'Sum').with({ period: cdk.Duration.hours(24) })], width: 6, height: 6 }),
    );
    return dashboard;
  }

  private createAlarms(prefix: string, config: LlmOpsAlarmConfig): void {
    const snsActions: cloudwatch.IAlarmAction[] = [];
    if (config.snsTopicArn) {
      const topic = cdk.aws_sns.Topic.fromTopicArn(this, 'AlarmTopic', config.snsTopicArn);
      snsActions.push(new cdk.aws_cloudwatch_actions.SnsAction(topic));
    }
    const mkAlarm = (id: string, name: string, ns: string, metric: string, threshold: number, evalPeriods: number) => {
      const alarm = new cloudwatch.Alarm(this, id, {
        alarmName: `${prefix}-${name}`, metric: new cloudwatch.Metric({ namespace: ns, metricName: metric, statistic: 'Sum', period: cdk.Duration.minutes(5) }),
        threshold, evaluationPeriods: evalPeriods, comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD, treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      });
      snsActions.forEach(a => alarm.addAlarmAction(a));
    };
    mkAlarm('ThrottleAlarm', 'llm-throttle-alarm', 'AWS/Bedrock', 'InvocationThrottles', config.throttleThreshold ?? 10, 2);
    mkAlarm('ErrorAlarm', 'llm-error-alarm', 'AWS/Bedrock', 'InvocationServerErrors', config.errorThreshold ?? 5, 2);
    mkAlarm('GuardrailAlarm', 'llm-guardrail-alarm', 'AWS/Bedrock/Guardrails', 'InvocationsIntervened', config.guardrailInterventionThreshold ?? 5, 2);

    // Latency alarm uses p90 statistic
    const latencyAlarm = new cloudwatch.Alarm(this, 'LatencyAlarm', {
      alarmName: `${prefix}-llm-latency-alarm`,
      metric: new cloudwatch.Metric({ namespace: 'AWS/Bedrock', metricName: 'InvocationLatency', statistic: 'p90', period: cdk.Duration.minutes(5) }),
      threshold: config.latencyThresholdMs ?? 30000, evaluationPeriods: 3,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD, treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    snsActions.forEach(a => latencyAlarm.addAlarmAction(a));
  }

  private toRetentionDays(days: number): logs.RetentionDays {
    const map: Record<number, logs.RetentionDays> = {
      1: logs.RetentionDays.ONE_DAY, 3: logs.RetentionDays.THREE_DAYS, 5: logs.RetentionDays.FIVE_DAYS,
      7: logs.RetentionDays.ONE_WEEK, 14: logs.RetentionDays.TWO_WEEKS, 30: logs.RetentionDays.ONE_MONTH,
      60: logs.RetentionDays.TWO_MONTHS, 90: logs.RetentionDays.THREE_MONTHS, 180: logs.RetentionDays.SIX_MONTHS,
      365: logs.RetentionDays.ONE_YEAR,
    };
    return map[days] ?? logs.RetentionDays.THREE_MONTHS;
  }
}
