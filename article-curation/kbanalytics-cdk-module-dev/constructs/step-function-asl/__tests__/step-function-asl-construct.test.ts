import * as cdk from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Template } from 'aws-cdk-lib/assertions';
import { StepFunctionAslConstruct } from '../step-function-asl-construct';
import { NamingUtil } from '../../../utils/naming';

describe('StepFunctionAslConstruct', () => {
  let app: cdk.App;
  let stack: cdk.Stack;
  let naming: NamingUtil;
  let role: iam.Role;

  beforeEach(() => {
    app = new cdk.App();
    stack = new cdk.Stack(app, 'TestStack');
    naming = new NamingUtil({ tenantId: 'test', envCode: 'd', regionCode: 'use1' });
    role = new iam.Role(stack, 'TestRole', {
      assumedBy: new iam.ServicePrincipal('states.amazonaws.com'),
    });
  });

  test('creates state machine and log group', () => {
    new StepFunctionAslConstruct(stack, 'TestSfn', {
      stateMachineName: 'test-d-use1-pipeline',
      role,
      definitionString: JSON.stringify({ StartAt: 'Pass', States: { Pass: { Type: 'Pass', End: true } } }),
      naming,
    });

    const template = Template.fromStack(stack);
    template.resourceCountIs('AWS::StepFunctions::StateMachine', 1);
    template.resourceCountIs('AWS::Logs::LogGroup', 1);
  });

  test('creates SSM parameter when ssmPrefix provided', () => {
    new StepFunctionAslConstruct(stack, 'TestSfn', {
      stateMachineName: 'test-d-use1-pipeline',
      role,
      definitionString: '{}',
      naming,
      ssmPrefix: '/test-d-use1',
    });

    const template = Template.fromStack(stack);
    template.resourceCountIs('AWS::SSM::Parameter', 1);
  });

  test('exposes state machine ARN and name', () => {
    const sfn = new StepFunctionAslConstruct(stack, 'TestSfn', {
      stateMachineName: 'test-d-use1-pipeline',
      role,
      definitionString: '{}',
    });

    expect(sfn.stateMachineArn).toBeDefined();
    expect(sfn.stateMachineName).toBe('test-d-use1-pipeline');
  });
});
