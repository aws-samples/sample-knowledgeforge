import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { BedrockManagedPromptConstruct } from '../bedrock-managed-prompt-construct';
import { NamingUtil } from '../../../utils/naming';

describe('BedrockManagedPromptConstruct', () => {
  let app: cdk.App;
  let stack: cdk.Stack;
  let naming: NamingUtil;

  beforeEach(() => {
    app = new cdk.App();
    stack = new cdk.Stack(app, 'TestStack');
    naming = new NamingUtil({ tenantId: 'test', envCode: 'd', regionCode: 'use1' });
  });

  test('creates Bedrock prompt and version', () => {
    new BedrockManagedPromptConstruct(stack, 'TestPrompt', {
      promptName: 'test-d-use1-kb_classification',
      description: 'Test classification prompt',
      modelId: 'anthropic.claude-sonnet-4-5-20250929-v1:0',
      systemText: 'You are a classifier.',
      userText: 'Classify: {{article_content}}',
      variables: ['article_content'],
      temperature: 0,
      maxTokens: 256,
      naming,
    });

    const template = Template.fromStack(stack);
    template.resourceCountIs('AWS::Bedrock::Prompt', 1);
    template.resourceCountIs('AWS::Bedrock::PromptVersion', 1);
  });

  test('creates SSM parameter when ssmPrefix provided', () => {
    new BedrockManagedPromptConstruct(stack, 'TestPrompt', {
      promptName: 'test-d-use1-kb_classification',
      description: 'Test prompt',
      modelId: 'anthropic.claude-sonnet-4-5-20250929-v1:0',
      systemText: 'System',
      userText: 'User',
      variables: ['article_content'],
      temperature: 0,
      maxTokens: 256,
      naming,
      ssmPrefix: '/test-d-use1',
    });

    const template = Template.fromStack(stack);
    template.resourceCountIs('AWS::SSM::Parameter', 1);
  });

  test('exposes prompt ARN and version ARN', () => {
    const prompt = new BedrockManagedPromptConstruct(stack, 'TestPrompt', {
      promptName: 'test-d-use1-kb_classification',
      description: 'Test',
      modelId: 'model-id',
      systemText: 'System',
      userText: 'User',
      variables: [],
      temperature: 0,
      maxTokens: 256,
    });

    expect(prompt.promptArn).toBeDefined();
    expect(prompt.versionArn).toBeDefined();
    expect(prompt.promptId).toBeDefined();
  });
});
