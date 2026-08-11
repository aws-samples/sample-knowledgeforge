import * as cdk from 'aws-cdk-lib';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';
import { NamingUtil } from '../../utils/naming';

/**
 * Bedrock Guardrail construct.
 *
 * Creates a Bedrock guardrail with content safety filters and contextual grounding,
 * plus a versioned snapshot.
 *
 * Pattern: <tenant_id>-<env_code>-<region_code>-<functionality>_guardrail
 * Example: example-s-euw1-content_pipeline_guardrail
 */
export interface ContentFilter {
  type: string;
  inputStrength: string;
  outputStrength: string;
}

export interface BedrockGuardrailConstructProps {
  /** Guardrail name */
  guardrailName: string;
  /** Description */
  description: string;
  /** Content safety filters */
  contentFilters: ContentFilter[];
  /** Contextual grounding threshold (0-1) */
  groundingThreshold: number;
  /** Message shown when input is blocked */
  blockedInputMessaging?: string;
  /** Message shown when output is blocked */
  blockedOutputsMessaging?: string;
  /** Optional NamingUtil for validation */
  naming?: NamingUtil;
  /** Optional SSM prefix for storing guardrail ID and version */
  ssmPrefix?: string;
}

export class BedrockGuardrailConstruct extends Construct {
  public readonly guardrailId: string;
  public readonly guardrailArn: string;
  public readonly guardrailVersion: string;

  constructor(scope: Construct, id: string, props: BedrockGuardrailConstructProps) {
    super(scope, id);

    if (props.naming) {
      const validation = props.naming.validateResourceName(props.guardrailName);
      if (!validation.isValid) {
        console.warn(`Bedrock guardrail naming validation warnings for "${props.guardrailName}":`);
        validation.errors.forEach((e) => console.warn(`  - ${e}`));
      }
    }

    const guardrail = new cdk.aws_bedrock.CfnGuardrail(this, 'Guardrail', {
      name: props.guardrailName,
      description: props.description,
      blockedInputMessaging: props.blockedInputMessaging || 'Input blocked by guardrail.',
      blockedOutputsMessaging: props.blockedOutputsMessaging || 'Output blocked by guardrail.',
      contentPolicyConfig: {
        filtersConfig: props.contentFilters.map(f => ({
          type: f.type,
          inputStrength: f.inputStrength,
          outputStrength: f.outputStrength,
        })),
      },
      contextualGroundingPolicyConfig: {
        filtersConfig: [
          { type: 'GROUNDING', threshold: props.groundingThreshold },
        ],
      },
    });

    this.guardrailId = guardrail.attrGuardrailId;
    this.guardrailArn = guardrail.attrGuardrailArn;

    const version = new cdk.aws_bedrock.CfnGuardrailVersion(this, 'Version', {
      guardrailIdentifier: guardrail.attrGuardrailArn,
      description: `Version: ${props.description}`,
    });

    this.guardrailVersion = version.attrVersion;

    if (props.ssmPrefix) {
      new ssm.StringParameter(this, 'GuardrailIdParam', {
        parameterName: `${props.ssmPrefix}/bedrock-guardrail/${id}/guardrail-id`,
        stringValue: guardrail.attrGuardrailId,
        description: `Bedrock guardrail ID for ${props.guardrailName}`,
      });
      new ssm.StringParameter(this, 'GuardrailVersionParam', {
        parameterName: `${props.ssmPrefix}/bedrock-guardrail/${id}/version`,
        stringValue: version.attrVersion,
        description: `Bedrock guardrail version for ${props.guardrailName}`,
      });
    }
  }
}
