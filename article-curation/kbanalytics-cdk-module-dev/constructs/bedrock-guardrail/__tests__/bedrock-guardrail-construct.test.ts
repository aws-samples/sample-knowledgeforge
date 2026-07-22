import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { BedrockGuardrailConstruct } from '../bedrock-guardrail-construct';
import { NamingUtil } from '../../../utils/naming';

describe('BedrockGuardrailConstruct', () => {
  let app: cdk.App;
  let stack: cdk.Stack;
  let naming: NamingUtil;

  beforeEach(() => {
    app = new cdk.App();
    stack = new cdk.Stack(app, 'TestStack');
    naming = new NamingUtil({ tenantId: 'test', envCode: 'd', regionCode: 'use1' });
  });

  test('creates guardrail with content filters and grounding', () => {
    new BedrockGuardrailConstruct(stack, 'TestGuardrail', {
      guardrailName: 'test-d-use1-kb_guardrail',
      description: 'Test guardrail',
      contentFilters: [
        { type: 'HATE', inputStrength: 'NONE', outputStrength: 'HIGH' },
        { type: 'INSULTS', inputStrength: 'NONE', outputStrength: 'HIGH' },
      ],
      groundingThreshold: 0.75,
      naming,
    });

    const template = Template.fromStack(stack);
    template.resourceCountIs('AWS::Bedrock::Guardrail', 1);
    template.resourceCountIs('AWS::Bedrock::GuardrailVersion', 1);
  });

  test('creates SSM parameters when ssmPrefix provided', () => {
    new BedrockGuardrailConstruct(stack, 'TestGuardrail', {
      guardrailName: 'test-d-use1-kb_guardrail',
      description: 'Test',
      contentFilters: [{ type: 'HATE', inputStrength: 'NONE', outputStrength: 'HIGH' }],
      groundingThreshold: 0.75,
      naming,
      ssmPrefix: '/test-d-use1',
    });

    const template = Template.fromStack(stack);
    template.resourceCountIs('AWS::SSM::Parameter', 2);
  });

  test('exposes guardrail ID, ARN, and version', () => {
    const gr = new BedrockGuardrailConstruct(stack, 'TestGuardrail', {
      guardrailName: 'test-d-use1-kb_guardrail',
      description: 'Test',
      contentFilters: [],
      groundingThreshold: 0.75,
    });

    expect(gr.guardrailId).toBeDefined();
    expect(gr.guardrailArn).toBeDefined();
    expect(gr.guardrailVersion).toBeDefined();
  });
});
