import * as cdk from 'aws-cdk-lib';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';
import * as path from 'path';
import * as fs from 'fs';
import {
  NamingUtil,
  S3VectorsConstruct,
  AppConfigConstruct,
  BedrockManagedPromptConstruct,
  BedrockGuardrailConstruct,
} from '@kbanalytics/cdk-constructs';

export interface ArticleCurationTenantStackProps extends cdk.StackProps {
  project: string;
  envName: string;
  envCode: string;
  regionCode: string;
  tenantId: string;
  tenantConfig: any;
  tenantOverrides: any;
  sharedPrefix: string;
  tags: Record<string, string>;
}

export class ArticleCurationTenantStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: ArticleCurationTenantStackProps) {
    super(scope, id, props);

    const { project, envName, envCode, regionCode, tenantId, tenantConfig, tenantOverrides, sharedPrefix, tags: globalTags } = props;
    const region = this.region;
    const accountId = this.account;
    const connectRegion = tenantConfig.connect_region;

    const naming = new NamingUtil({ tenantId, envCode, regionCode });
    const ssmPrefix = `/${naming.getPrefix()}/${connectRegion}`;
    const sharedSsmPrefix = `/shared-${envCode}-${regionCode}`;
    const sharedNaming = new NamingUtil({ tenantId: 'shared', envCode, regionCode });

    // ── Shared KMS Key (imported by alias for secret encryption) ─────────
    const sharedKmsKeyAlias = `alias/${sharedNaming.prefixName(tenantConfig.s3?.source_kms_alias)}`;
    const sharedKmsKey = cdk.aws_kms.Key.fromLookup(this, 'SharedKmsKey', {
      aliasName: sharedKmsKeyAlias,
    });

    // Tags
    Object.entries(globalTags).forEach(([k, v]) => cdk.Tags.of(this).add(k, v));
    cdk.Tags.of(this).add('Tenant', tenantId);
    cdk.Tags.of(this).add('Region', connectRegion);

    // ── ServiceNow API Key Secret ────────────────────────────────────────
    // Created per tenant per connect region, encrypted with the shared KMS key.
    // The secret value must be populated manually after deployment.
    const snApiKeySecret = new cdk.aws_secretsmanager.Secret(this, 'SnApiKeySecret', {
      secretName: `${naming.prefixName(`${connectRegion}-kbcuration-servicenow-api-key`)}`,
      description: `ServiceNow API key for tenant ${tenantId} in ${connectRegion}`,
      encryptionKey: sharedKmsKey,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });
    const snApiKeySecretArn = snApiKeySecret.secretArn;

    // Tags
    Object.entries(globalTags).forEach(([k, v]) => cdk.Tags.of(this).add(k, v));
    cdk.Tags.of(this).add('Tenant', tenantId);
    cdk.Tags.of(this).add('Region', connectRegion);

    // ── S3 Vectors ───────────────────────────────────────────────────────
    new S3VectorsConstruct(this, 'Vectors', {
      vectorBucketName: naming.prefixName(`${connectRegion}-kb-vectors`),
      indexName: `${tenantId}-${connectRegion}-kb-vectors`,
      dimension: tenantConfig.models.embedding_dimensions,
      naming,
      ssmPrefix,
    });

    // ── Bedrock Prompts (per-tenant) ─────────────────────────────────────
    const promptsDir = path.join(__dirname, '..', 'tenants', tenantId, 'prompts');
    const loadPrompt = (name: string) => {
      const filePath = path.join(promptsDir, name);
      if (!fs.existsSync(filePath)) {
        throw new Error(`Prompt file not found: ${filePath}. Each tenant must have its own prompts in tenants/${tenantId}/prompts/`);
      }
      return fs.readFileSync(filePath, 'utf-8').trim();
    };

    const promptKeys = ['classification', 'quality', 'enrichment', 'enrichment_light', 'post_scoring', 'retirement_detection'] as const;
    const promptMeta: Record<string, { desc: string; vars: string[] }> = {
      classification: { desc: 'Classifies KB articles', vars: ['article_content'] },
      quality: { desc: 'Quality scoring', vars: ['classification', 'references', 'article_content', 'criteria'] },
      enrichment: { desc: 'Deep paragraph enrichment', vars: ['classification', 'scores', 'issues', 'article_html'] },
      enrichment_light: { desc: 'Light paragraph enrichment for high-quality articles', vars: ['classification', 'scores', 'issues', 'article_html'] },
      post_scoring: { desc: 'Post-enrichment scoring', vars: ['classification', 'original_content', 'article_content'] },
      retirement_detection: { desc: 'Detects outdated technology for retirement', vars: ['classification', 'article_content'] },
    };

    const promptArns: Record<string, string> = {};

    for (const key of promptKeys) {
      const inf = tenantConfig.inference[key];

      const prompt = new BedrockManagedPromptConstruct(this, `Prompt_${key}`, {
        promptName: naming.prefixName(`${connectRegion}-kb_${key}`),
        description: promptMeta[key].desc,
        modelId: tenantConfig.models.llm_model_id,
        systemText: loadPrompt(`${key}.system.txt`),
        userText: loadPrompt(`${key}.user.txt`),
        variables: promptMeta[key].vars,
        temperature: inf.temperature,
        maxTokens: inf.max_tokens,
        naming,
        ssmPrefix,
      });
      promptArns[key] = prompt.versionArn;
    }

    // ── Bedrock Guardrail (per-tenant) ───────────────────────────────────
    const grConfig = tenantConfig.guardrail;
    const hasGuardrailOverride = Array.isArray(grConfig?.content_filters) && grConfig.content_filters.length > 0;

    const guardrail = new BedrockGuardrailConstruct(this, 'Guardrail', {
      guardrailName: naming.prefixName(`${connectRegion}-article_curation_guardrail`),
      description: 'Content safety and contextual grounding',
      contentFilters: (hasGuardrailOverride ? grConfig.content_filters : tenantConfig.guardrail.content_filters || []).map((f: any) => ({
        type: f.type, inputStrength: f.input_strength || f.inputStrength || 'NONE', outputStrength: f.output_strength || f.outputStrength || 'HIGH',
      })),
      groundingThreshold: grConfig?.grounding_threshold ?? tenantConfig.guardrail?.grounding_threshold ?? 0.75,
      naming,
      ssmPrefix,
    });

    // ── Tenant AppConfig ─────────────────────────────────────────────────

    new AppConfigConstruct(this, 'TenantAppConfig', {
      appName: naming.prefixName(`${connectRegion}-article_curation_config`),
      profileName: 'tenant-config',
      envName,
      configContent: {
        // Tenant-specific overrides first: any key explicitly defined in the
        // tenant's yaml gets included. Only what the tenant defines, not base.yaml.
        ...tenantOverrides,

        // CDK-computed fields overlay on top (cannot be overridden by tenant yaml)
        _metadata: {
          tenant_id: tenantId,
          environment: envName,
          region,
        },
        servicenow: {
          ...(tenantOverrides.servicenow || {}),
          api_key_secret_arn: snApiKeySecretArn,
        },
        resources: {
          table_name: sharedNaming.prefixName(tenantConfig.dynamodb?.article_metadata?.name),
          job_status_table: sharedNaming.prefixName(tenantConfig.dynamodb?.pipeline_job_status?.name || 'pipeline_job_status'),
          pipeline_bucket: sharedNaming.prefixName(tenantConfig.s3?.pipeline_bucket?.name || 'kb-pipeline'),
          source_bucket: tenantConfig.s3?.source_bucket?.name || '',
          vector_bucket: naming.prefixName(`${connectRegion}-kb-vectors`),
          vector_index: `${tenantId}-${connectRegion}-kb-vectors`,
          tenant_id: tenantId,
          connect_region: connectRegion,
        },
        prompts: promptArns,
        guardrail: {
          guardrail_id: guardrail.guardrailId,
          guardrail_version: guardrail.guardrailVersion,
        },
        quality_thresholds: tenantConfig.quality_thresholds,
      },
      naming,
      ssmPrefix,
    });
  }
}
