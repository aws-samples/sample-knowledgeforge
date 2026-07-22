# KnowledgeForge

A closed-loop knowledge base lifecycle platform for IT Service Management (ITSM). KnowledgeForge mines reusable knowledge out of resolved incident tickets, turns it into new KB and RCA articles, and continuously curates both new and existing articles - classifying, deduplicating, quality-scoring, and enriching them before routing them to ServiceNow for knowledge-manager review.

The platform is built from two complementary subsystems:

| Subsystem | Role | Compute model | Location |
|-----------|------|---------------|----------|
| **Article Generation** | Upstream. Mines clustered incident tickets into new KB + RCA articles. | ECS Fargate (long-running, bursty) | [`article-generation/`](article-generation/) |
| **Article Curation** | Downstream. Classifies, deduplicates, quality-scores, and enriches existing + newly generated articles. | Lambda + Step Functions (event-driven, parallel) | [`article-curation/`](article-curation/) |

Both subsystems are multi-tenant, run on `eu-west-1`, and share Amazon Bedrock (Claude Sonnet 4.5 for generation/enrichment, Titan Embed v2 for embeddings) and Amazon S3 Vectors for semantic search.

---

## The closed loop

```
        Resolved incident tickets (clustered by theme)
                        │
                        ▼
        ┌───────────────────────────────────┐
        │          ARTICLE GENERATION        │
        │  (ECS Fargate)                     │
        │  • RAG grounding from S3 Vectors   │
        │  • Bedrock streaming generation    │
        │  • Writes KB + RCA articles to S3  │
        └───────────────────────────────────┘
                        │  new articles (JSON) land in source S3
                        ▼
        ┌───────────────────────────────────┐
        │          ARTICLE CURATION          │
        │  (Lambda + Step Functions)         │
        │  • Classify + embed                │
        │  • Semantic dedup (S3 Vectors)     │
        │  • 10-dimension quality scoring    │
        │  • LLM enrichment + re-scoring     │
        └───────────────────────────────────┘
                        │  enriched articles
                        ▼
        ServiceNow - knowledge-manager review (human in the loop)
```

Article Curation also embeds every article it processes into per-tenant S3 Vectors indexes, which Article Generation reuses as RAG grounding context - closing the loop so newly generated content is informed by the existing, curated knowledge base.

---

## Article Generation (upstream)

Mines new knowledge from clustered incident tickets.

- **Input** - theme-based ticket clusters (`themes.json`) dropped into a source S3 bucket, which triggers processing automatically via S3 event notification.
- **RAG grounding** - retrieves similar existing articles from per-tenant S3 Vectors indexes before generating, so new articles are consistent with the existing KB.
- **Generation** - Amazon Bedrock (Claude Sonnet 4.5) streams two document types:
  - **KB articles** - Summary, Symptoms, Root Cause, Resolution Steps, Prevention, Related Topics.
  - **RCA documents** - Executive Summary, Problem Description, Customer Impact, Root Cause, 5-Why Analysis, Workaround & Resolution, Corrective/Preventive Actions, Key Events Timeline, Cause Code.
- **Compute** - runs as an ECS Fargate task (chosen over Lambda for long-running, bursty generation work that exceeds Lambda limits).
- **Output** - KB and RCA articles written to S3 as `{tenant}/{article_type}/{date}/{uuid}.json`, which becomes input for Article Curation.

See [`article-generation/README.md`](article-generation/README.md) for deploy steps.

## Article Curation (downstream)

Curates both newly generated articles and the existing KB at scale.

- **Ingestion + change detection** - a File Enumerator Lambda discovers tenants from the Glue data catalog and detects new/changed articles. Two modes: `s3_scan` (sandbox) and `glue_catalog` (dev/prod), the latter using a single Athena query across all tenants.
- **Classify & embed** - Claude Sonnet 4.5 classifies each article (SOP / FAQ / Troubleshooting / RCA / Runbook); Titan Embed v2 (1024-dim) embeddings are stored in per-tenant S3 Vectors.
- **Deduplicate** - cosine-similarity matching against the vector index, with freshness-swap logic to retire stale duplicates.
- **Quality score + enrich** - 10-dimension weighted quality scoring with threshold gating, placeholder-based LLM enrichment that preserves media/heading tags, and post-enrichment re-scoring to guarantee improvement.
- **Human in the loop** - enriched articles are routed to ServiceNow for knowledge-manager review; approval/rejection flows back in via an SQS queue, and rejected content is automatically removed from the vector index.
- **Orchestration** - SQS FIFO batching feeds a two-phase Step Functions Distributed Map, with circuit-breaker and dead-letter handling for fault tolerance.

See [`article-curation/README.md`](article-curation/README.md) for the full architecture, phase breakdown, and config layering. End-to-end test procedures are in [`article-curation/TESTING_INSTRUCTIONS.md`](article-curation/TESTING_INSTRUCTIONS.md).

---

## Multi-tenant isolation

Every tenant is isolated across the whole platform: its own AppConfig profile, S3 Vectors bucket/index, Bedrock managed prompts, Bedrock guardrail, and KMS key. Tenant discovery is driven from the data catalog rather than static configuration, so onboarding a tenant does not require a code change.

## Repository layout

```
article-generation/        Upstream - incident-to-article generation (ECS Fargate)
  cdk/                CDK app (VPC, SQS, DynamoDB, ECR, ECS, guardrail)
  pipeline/           Container application (generation logic, prompts, config)

article-curation/          Downstream - classify, dedup, score, enrich (Lambda + Step Functions)
  infra/              CDK app (shared stack + per-tenant stacks, layered YAML config)
  lambda/             Lambda functions (file_enumerator, dispatcher, classify_embed,
                      dedup, sn_task_creator, webhook_handler, shared layer)
  step-function/      Step Functions ASL definition
```

## Tech stack

- **AI/ML** - Amazon Bedrock (Claude Sonnet 4.5, Titan Embed v2), Amazon S3 Vectors, Bedrock Guardrails
- **Compute** - AWS Lambda, AWS Step Functions (Distributed Map), Amazon ECS Fargate
- **Data** - Amazon DynamoDB, Amazon S3, AWS Glue + Amazon Athena
- **Messaging** - Amazon SQS (FIFO + standard, with DLQs), Amazon EventBridge
- **IaC** - AWS CDK (TypeScript)
- **Integration** - ServiceNow (knowledge-manager review)

> Note: this repository is a sanitized reference copy. Account IDs, KMS key IDs, tenant identifiers, and bucket names are placeholders.
