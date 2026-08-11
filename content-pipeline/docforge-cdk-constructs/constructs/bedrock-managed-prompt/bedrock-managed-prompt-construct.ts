import * as cdk from 'aws-cdk-lib';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';
import { NamingUtil } from '../../utils/naming';

/**
 * Bedrock Managed Prompt construct.
 *
 * Creates a Bedrock prompt with CHAT template type and a versioned snapshot.
 * Supports system + user message templates with input variables.
 *
 * Pattern: <tenant_id>-<env_code>-<region_code>-kb_<prompt_type>
 * Example: example-s-euw1-kb_classification
 */
export interface BedrockManagedPromptProps {
  /** Prompt name */
  promptName: string;
  /** Description of what the prompt does */
  description: string;
  /** Bedrock model ID for the prompt */
  modelId: string;
  /** System message text */
  systemText: string;
  /** User message template text */
  userText: string;
  /** Input variable names (extracted from {{var}} placeholders) */
  variables: string[];
  /** LLM temperature (0-1) */
  temperature: number;
  /** Max output tokens */
  maxTokens: number;
  /** Enable prompt caching on the system prompt (default: true) */
  enablePromptCaching?: boolean;
  /** Optional NamingUtil for validation */
  naming?: NamingUtil;
  /** Optional SSM prefix for storing prompt version ARN */
  ssmPrefix?: string;
}

export class BedrockManagedPromptConstruct extends Construct {
  public readonly promptArn: string;
  public readonly versionArn: string;
  public readonly promptId: string;

  constructor(scope: Construct, id: string, props: BedrockManagedPromptProps) {
    super(scope, id);

    if (props.naming) {
      const validation = props.naming.validateResourceName(props.promptName);
      if (!validation.isValid) {
        console.warn(`Bedrock prompt naming validation warnings for "${props.promptName}":`);
        validation.errors.forEach((e) => console.warn(`  - ${e}`));
      }
    }

    const enableCaching = props.enablePromptCaching !== false; // default: true

    // System block: text + optional cachePoint after it
    const systemBlocks: any[] = [{ text: props.systemText }];
    if (enableCaching) {
      systemBlocks.push({ cachePoint: { type: 'default' } });
    }

    const prompt = new cdk.aws_bedrock.CfnPrompt(this, 'Prompt', {
      name: props.promptName,
      description: props.description,
      defaultVariant: 'default',
      variants: [{
        name: 'default',
        modelId: props.modelId,
        templateType: 'CHAT',
        templateConfiguration: {
          chat: {
            system: systemBlocks,
            messages: [{ role: 'user', content: [{ text: props.userText }] }],
            inputVariables: props.variables.map(v => ({ name: v })),
          },
        },
        inferenceConfiguration: {
          text: { temperature: props.temperature, maxTokens: props.maxTokens },
        },
      }],
    });

    this.promptId = prompt.ref;
    this.promptArn = prompt.attrArn;

    // Hash the prompt content to force new version when content changes
    const crypto = require('crypto');
    const contentHash = crypto.createHash('md5')
      .update(props.systemText + props.userText + props.variables.join(','))
      .digest('hex').substring(0, 8);

    const version = new cdk.aws_bedrock.CfnPromptVersion(this, 'Version', {
      promptArn: prompt.attrArn,
      description: `Version: ${props.description} [${contentHash}]`,
    });

    this.versionArn = version.attrArn;

    if (props.ssmPrefix) {
      new ssm.StringParameter(this, 'VersionArnParam', {
        parameterName: `${props.ssmPrefix}/bedrock-prompt/${id}/version-arn`,
        stringValue: version.attrArn,
        description: `Bedrock prompt version ARN for ${props.promptName}`,
      });
    }
  }
}
