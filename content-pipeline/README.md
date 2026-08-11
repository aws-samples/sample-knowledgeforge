# Article Curation Pipeline

Pipeline to curate ITSM knowledge base articles. Ingests KB articles as JSON from a source S3 bucket, classifies them (SOP/FAQ/Troubleshooting/RCA/Runbook), generates embeddings, deduplicates, scores quality across 10 dimensions, and enriches the content using Bedrock LLMs. Enriched articles are sent to TicketSystem for knowledge manager review.

Supports multiple tenants and three source systems: ITSM_KB, CONTENT_GENERATOR_IC, CONTENT_GENERATOR_RCA.

## Architecture

Two CDK stacks per environment:

- **Shared stack** - DynamoDB tables (`article_metadata` with `pipeline-status-index` and `ritm-number-index` GSIs, `pipeline_job_status`), S3 source bucket, S3 pipeline bucket, SQS FIFO queue + DLQ (batch processing), SQS standard queue + DLQ (approval/rejection), Lambda functions (File Enumerator, WriteManifest, Classify & Embed, Dedup, Dispatcher, SN Task Creator, Webhook Handler), Step Function, EventBridge schedule, shared AppConfig (with outbound field mapping), LLMOps monitoring, shared Bedrock guardrail.
- **Tenant stack** (one per tenant) - S3 Vectors bucket/index, 4 Bedrock managed prompts (classification, quality, enrichment, post-scoring), tenant-specific Bedrock guardrail, tenant AppConfig profile.

No cross-stack references. Tenant stacks compute shared resource names using `NamingUtil` at CDK synth time.

## How it works

```
EventBridge (daily schedule)
  │
  ▼
File Enumerator Lambda (REFACTORED + OPTIMIZED)
  ├── Modular architecture (7 focused modules):
  │     • handler.py - Main orchestrator with tenant filtering
  │     • tenant_discovery.py - Discover tenants from Glue
  │     • change_detection.py - Single Athena query for all tenants
  │     • article_ingestion.py - Load articles and write DynamoDB
  │     • batch_processor.py - Batch files and send to SQS
  │     • tenant_context.py - Thread-safe tenant config
  │     • utils.py - Shared utilities
  │
  ├── Discovers tenants from Glue catalog (region-aware)
  │
  ├── SINGLE Athena query optimization (NEW):
  │     • ONE query for ALL tenants instead of N separate queries
  │     • Builds combined SQL with OR conditions per tenant
  │     • Fetches last run timestamps in parallel
  │     • Groups results by tenant_id after fetch
  │     • N times faster, N times cheaper, simpler code
  │
  ├── Change detection per tenant:
  │     s3_scan mode (sandbox): scan all files, hash-based
  │     glue_catalog mode (dev/prod): query Athena with metadata (no S3 reads!)
  │       • Fetches 12 metadata fields (no full_text - read from source S3)
  │       • Processes ~175 tenants in 15-min timeout
  │
  ├── Tenant filtering support (NEW):
  │     • Single tenant: {"tenant_id": "orgAlpha"}
  │     • Multiple tenants: {"tenant_id": ["orgAlpha", "contoso"]}
  │     • All tenants: {} (default)
  │     • Validates against Glue catalog
  │
  ├── Picks up stuck articles (RAW/CLASSIFIED/EMBEDDED) from previous failed runs
  ├── Problem Finder: queries DynamoDB GSI for new IC/RCA articles (incremental)
  ├── Batches changed files (configurable batch_size)
  └── Sends batches to SQS FIFO Queue
        │
        ▼
  SQS FIFO Queue (sequential processing)
        │
        ▼
  Dispatcher Lambda (per message, batch_size=1)
  ├── Starts Step Function execution
  ├── Polls until complete (max 13 min)
  ├── On success: resets failure counter
  └── On failure: resets articles to RAW, sends to DLQ, circuit breaker
        │
        ▼
  Step Function (per batch)
  ├── Write Manifest (writes articles to S3 manifest)
  ├── Phase 1: Classify & Embed (Distributed Map reads manifest)
  ├── Phase 2: Dedup + Quality + Enrich (Distributed Map)
  └── Aggregate Stats → pipeline_job_status table (per batch)
```

### S3 Layout

```
Source Bucket (data team's curated-unstructured-s3)
  {prefix}/{tenant_id}/{region}/{article_id}.json    One JSON per article, DW schema
  e.g. tenant_partitioning/itsm/snow/kb_articles/orgalpha/eu-west-1/DOC0001001.json

Pipeline Bucket (pipeline working data)
  {tenant_id}/generated/*.json     Lean output payload for TicketSystem
  {tenant_id}/generated/*.html     Enriched HTML content
  {tenant_id}/pipeline-manifests/  Job manifests for Step Function

Problem Finder Source Bucket (shared-{env}-euw1-knowledge-output)
  {tenant_id}/{region}/ic/{date}/{uuid}.json   Problem Finder IC articles
  {tenant_id}/{region}/rca/{date}/{uuid}.json  Problem Finder RCA articles
```

### Data Tracking

- **`article_metadata`**: Per-article records. PK=`tenant_id` (UUID), SK=`src_kb_article_id`. Stores `tenant_code` and `home_region` as attributes. Pipeline status in `pipeline_status`. DW metadata in lowercase columns. GSI: `pipeline-status-index`.
- **`pipeline_job_status`**: Per-tenant-per-batch records. PK=`tenant_id` (UUID), SK=`job_id#batch_id`. Includes `parent_job_id`, `status_breakdown`, `language_breakdown`.

## Phases

**Change Detection** - Two modes. `s3_scan` (sandbox): scans all S3 files, computes content hashes, detects new/changed/deleted articles. `glue_catalog` (dev/prod): **OPTIMIZED** - uses SINGLE Athena query for all tenants instead of N separate queries. Builds combined SQL with OR conditions for each tenant + their respective timestamps. Fetches metadata (12 fields, no full_text) and groups results by tenant_id. Achieves N times faster execution and N times lower Athena costs. Also picks up stuck articles from previous failed runs. Supports ~175 tenants in 15-minute Lambda timeout.

**Classify & Embed** - Claude Sonnet 4.5 classification + Titan Embed V2 (1024-dim) stored in S3 Vectors.

**Dedup, Quality & Enrich** - Cosine similarity dedup, 10-dimension quality scoring, placeholder-based enrichment preserving media/headings, post-enrichment re-scoring.

## SQS Batch Processing

File Enumerator batches changed files and sends to SQS FIFO. Dispatcher consumes one message at a time, starts a Step Function, polls until done. Failed batches go to DLQ with articles reset to RAW. Circuit breaker halts after N consecutive failures (configurable).

## Approval/Rejection Flow

After enrichment, articles with `GENERATED` status trigger the SN Task Creator via DynamoDB Streams. It reads the output JSON from S3, applies outbound field mapping (DW → SN names via shared AppConfig), and POSTs to TicketSystem's Automation API with `doc_payload` + `kb_score` payloads. Auth uses `x-api-key` from Secrets Manager. ITSM_KB articles send `article_id` + enriched text; Problem Finder articles send full metadata. Status updates to `REQUEST_CREATED` with RITM details stored in DynamoDB. A `review_limit` config controls how many articles are sent per job per tenant (-1 = disabled, 0 = all, N = send N). Reads from tenant AppConfig first, falls back to env var.

Another team sends approval/rejection messages to the `doc_approval_queue` SQS queue. The Webhook Handler Lambda consumes these messages, looks up the article by RITM number (via `ritm-number-index` GSI), and updates status to `KM_APPROVED` or `KM_REJECTED` with approval details. **When an article is rejected, the webhook handler automatically deletes its vector from S3 Vectors** to keep the vector index clean and prevent rejected content from appearing in similarity searches.

## Project Structure

```
infra/
  bin/app.ts                        CDK entry point
  lib/content-pipeline-shared-stack.ts   Shared resources
  lib/content-pipeline-tenant-stack.ts   Per-tenant resources
  config/_defaults/base.yaml        Base config
  config/global-environments.yaml   Accounts, regions, tags
  tenants/{id}/prompts/             Prompt files per tenant
  tenants/{id}/config/              Tenant overrides

lambda/
  file_enumerator/                  REFACTORED - Modular architecture:
    handler.py                      Main orchestrator with tenant filtering
    tenant_discovery.py             Discover tenants from Glue catalog
    change_detection.py             Single Athena query for all tenants
    article_ingestion.py            Load articles and write DynamoDB
    batch_processor.py              Batch files and send to SQS
    tenant_context.py               Thread-safe tenant config container
    utils.py                        Shared utility functions
  dispatcher/                       SQS → Step Function + circuit breaker
  classify_embed/                   Phase 1
  dedup/                            Phase 2
  ticket_submitter/                  DynamoDB stream → TicketSystem
  webhook_handler/                  SQS approval queue → DynamoDB status update
  shared/python/                    Shared layer

step-function/content-pipeline-pipeline.json

```

## Config

Layered YAML - `base.yaml` → `global-environments.yaml` → `tenants/{id}/config/{env}/{region}.yaml`. Deep merge, tenant wins.

## Deploy

```bash
cd infra && npm install && npx cdk deploy --all
```

New tenant? Create `infra/tenants/{id}/` with config + prompts. CDK auto-discovers.
