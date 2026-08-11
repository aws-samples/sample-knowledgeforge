import * as cdk from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as sfn from 'aws-cdk-lib/aws-stepfunctions';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';
import { NamingUtil } from '../../utils/naming';

/**
 * Step Function construct that accepts an ASL JSON definition.
 *
 * Use this when the state machine is defined as a JSON template with
 * placeholder substitution, rather than CDK definition chains.
 */
export interface StepFunctionAslConstructProps {
  /** State machine name */
  stateMachineName: string;
  /** IAM role for the state machine */
  role: iam.IRole;
  /** ASL definition as a JSON string (placeholders already replaced) */
  definitionString: string;
  /** Log level for execution logging */
  logLevel?: string;
  /** Include execution data in logs */
  includeExecutionData?: boolean;
  /** Log retention in days (default: 30) */
  logRetentionDays?: number;
  /** Optional NamingUtil for validation and SSM */
  naming?: NamingUtil;
  /** Optional SSM prefix */
  ssmPrefix?: string;
}

export class StepFunctionAslConstruct extends Construct {
  public readonly stateMachineArn: string;
  public readonly stateMachineName: string;

  constructor(scope: Construct, id: string, props: StepFunctionAslConstructProps) {
    super(scope, id);

    if (props.naming) {
      const validation = props.naming.validateResourceName(props.stateMachineName);
      if (!validation.isValid) {
        console.warn(`Step Function naming validation warnings for "${props.stateMachineName}":`);
        validation.errors.forEach((e) => console.warn(`  - ${e}`));
      }
    }

    const retentionMap: Record<number, logs.RetentionDays> = {
      7: logs.RetentionDays.ONE_WEEK, 14: logs.RetentionDays.TWO_WEEKS,
      30: logs.RetentionDays.ONE_MONTH, 60: logs.RetentionDays.TWO_MONTHS,
      90: logs.RetentionDays.THREE_MONTHS, 180: logs.RetentionDays.SIX_MONTHS,
      365: logs.RetentionDays.ONE_YEAR,
    };

    const logGroup = new logs.LogGroup(this, 'LogGroup', {
      logGroupName: `/aws/vendedlogs/states/${props.stateMachineName}`,
      retention: retentionMap[props.logRetentionDays ?? 30] ?? logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const stateMachine = new sfn.CfnStateMachine(this, 'StateMachine', {
      stateMachineName: props.stateMachineName,
      roleArn: props.role.roleArn,
      definitionString: props.definitionString,
      loggingConfiguration: {
        destinations: [{ cloudWatchLogsLogGroup: { logGroupArn: logGroup.logGroupArn } }],
        includeExecutionData: props.includeExecutionData ?? true,
        level: props.logLevel || 'ERROR',
      },
    });

    const roleCfn = props.role.node.defaultChild;
    if (roleCfn) stateMachine.addDependency(roleCfn as cdk.CfnResource);
    stateMachine.addDependency(logGroup.node.defaultChild as cdk.CfnResource);

    this.stateMachineArn = stateMachine.attrArn;
    this.stateMachineName = props.stateMachineName;

    if (props.ssmPrefix) {
      new ssm.StringParameter(this, 'ArnParam', {
        parameterName: `${props.ssmPrefix}/step-functions/${id}/arn`,
        stringValue: stateMachine.attrArn,
        description: `Step Function ARN for ${props.stateMachineName}`,
      });
    }
  }
}
