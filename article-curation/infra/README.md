# Article Curation Pipeline - CDK Infrastructure

This is the infrastructure code for the article curation pipeline. It deploys everything needed to ingest ServiceNow KB articles, run them through classification, dedup, quality scoring, and enrichment via Bedrock LLMs, and produce enriched articles for knowledge manager review.

## What gets deployed

### Shared resources (one per environment)

| Resource | Isolation strategy |
|----------|-------------------|
| Step Function | Tenant context passed as execution input |
| Lambda functions (change_detection, classify_embed, dedup) | Same code, tenant-aware via input payload |
| Lambda function (discover_tenants) | Reads tenant registry, returns active tenant list for pipeline |
| Lambda shared layer | Common utilities (docx parsing, html_utils, logger, etc.) |
| AppConfig Extension layer | AWS-managed layer, runs config server inside Lambda |
| DynamoDB - article_metadata | Row-level isolation via `tenant_id` partition key |
| DynamoDB - pipeline_job_status | Row-level isolation via `tenant_id` partition key |
| DynamoDB - tenant_registry | Stores active tenants, read by discover_tenants Lambda at pipeline start |
| IAM role - Lambda | Scoped to access tenant resources via naming patterns |
| IAM role - Step Function | Scoped for Lambda invoke, S3, DynamoDB, child executions |
| Bedrock model access | Same model endpoints (Claude Sonnet + Titan Embed), `tenant_id` in logs |
| CloudWatch logs | Filtered by `tenant_id` in structured logs |
| LLMOps observability | Invocation logging, dashboard, alarms (via LlmOps construct) |

### Per-tenant resources

| Resource | Isolation strategy |
|----------|-------------------|
| S3 bucket (documents) | Separate bucket per tenant |
| S3 Vectors / vector store | Separate vector bucket + index per tenant |
| AppConfig profile | Tenant-specific thresholds, dimensions, weights |
| Bedrock Managed Prompts | Tenant-specific prompt versions for classification, quality, enrichment, post-scoring |
| Bedrock Guardrails | Tenant-specific safety policies, grounding thresholds |

## Project structure

```
infra/
  .env                          # Environment identity (region, account, model IDs)
  cdk.json                      # CDK app config
  config/
    tenants.json                # Tenant list + per-tenant overrides
    shared-config.json          # Pipeline config (inference params, quality dims, dedup, guardrails)
    llmops-config.json          # LLMOps alarm thresholds, log retention
  prompts/
    classification.system.txt   # System prompt for article classification
    classification.user.txt     # User prompt template
    quality.system.txt          # Quality scoring system prompt
    quality.user.txt
    enrichment.system.txt       # Enrichment system prompt
    enrichment.user.txt
    post_scoring.system.txt     # Post-enrichment re-scoring
    post_scoring.user.txt
  lib/
    article-curation-stack.ts        # Main CDK stack
    llmops-construct.ts         # Reusable LLMOps construct (logging, dashboard, alarms)
  bin/
    app.ts                      # CDK app entry point
```

## Configuration

Three layers of config. Nothing is hardcoded in the CDK code.

### `.env` - environment identity

Static values that change per deployment environment (sandbox vs production). AWS account, region, model ARNs, bucket names.

### `config/shared-config.json` - pipeline tuning

Runtime config that all tenants share. Gets pushed to Shared AppConfig on every deploy.

- `models` - embedding dimensions, normalization
- `inference` - temperature and max_tokens per prompt (classification, quality, enrichment, post_scoring)
- `truncation` - max character limits per phase
- `quality_dimensions` - dimension names, weights, descriptions
- `dedup` - cosine distance threshold, top-k for similarity search
- `pipeline` - worker concurrency, throttle settings
- `change_detection` - protected statuses that skip reprocessing
- `guardrail` - content filter types/strengths, grounding threshold
- `appconfig_extension_layer_version` - AWS AppConfig Extension layer version
- `sfn_log_retention_days` - Step Function log retention

### `config/tenants.json` - per-tenant overrides

Each tenant can override quality thresholds and Step Function concurrency settings.

```json
[
  {
    "tenant_id": "example",
    "description": "Article Curation PoC tenant",
    "overrides": {
      "quality_threshold_itsm_kb": 60,
      "sfn_max_concurrency": 40
    }
  }
]
```

### `config/llmops-config.json` - observability settings

Alarm thresholds for Bedrock throttles, latency, errors, and guardrail interventions. Log retention periods for invocation logs.

## Setup

```bash
cd infra
npm install
```

## Deploy

```bash
# First time - bootstrap CDK in your account/region
npx cdk bootstrap

# Deploy
npx cdk deploy
```

## Adding a new tenant

1. Add an entry to `config/tenants.json` with the tenant_id and any overrides
2. Run `npx cdk deploy`
3. The tenant gets its own S3 bucket, vectors store, prompts, guardrail, and AppConfig profile
4. Shared resources (Lambdas, Step Function, DynamoDB) are reused - tenant isolation is via `tenant_id` in the payload and partition key

## Changing prompts

Edit the `.txt` files in `prompts/`. Each prompt has a system and user template. On deploy, CDK creates a new Bedrock Managed Prompt version per tenant. Old versions stay available for rollback.

Future: per-tenant prompt customization via feedback loops will override the shared templates at the tenant level.

## Changing quality dimensions or thresholds

Edit `config/shared-config.json`. The `quality_dimensions` section controls which dimensions are scored and their weights. Set a dimension's weight to `0.00` to disable it.

Per-tenant quality thresholds (minimum score to pass) go in `config/tenants.json` under `overrides`.

## LLMOps construct

`lib/llmops-construct.ts` is a reusable CDK construct for LLM observability. Not tied to article-curation - any LLM use-case can import it.

What it creates:
- Bedrock model invocation logging (CloudWatch + S3 for large payloads)
- CloudWatch dashboard - invocation counts, input/output tokens, latency percentiles (avg/p90/p99), throttles, errors, guardrail invocations vs interventions
- CloudWatch alarms - throttles, latency, server errors, guardrail blocks
- `grantBedrockInvoke()` helper to attach Bedrock IAM policies to any role

```typescript
const llmops = new LlmOpsConstruct(this, 'LlmOps', { ... });
llmops.grantBedrockInvoke(lambdaRole, modelArns, guardrailArn);
```
