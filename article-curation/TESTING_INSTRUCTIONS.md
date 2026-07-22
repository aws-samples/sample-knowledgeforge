# Testing the Pipeline

Guide to run the KB Curation Pipeline end-to-end, from small smoke tests to full multi-tenant load tests.

## AWS Environment

- Account: `777777777777` (sandbox)
- Region: `eu-west-1`
- Source bucket: `shared-s-euw1-curated-unstructured-s3-v3`
- Source prefix: `tenant_partitioning/itsm/snow/kb_articles`
- Pipeline bucket: `shared-s-euw1-kb-pipeline-v3`
- Article table: `shared-s-euw1-article_metadata`
- Job status table: `shared-s-euw1-pipeline_job_status`
- Batch queue: `shared-s-euw1-kb_pipeline_batch_queue.fifo`
- DLQ: `shared-s-euw1-kb_pipeline_batch_queue_dlq.fifo`

Resource naming convention: `shared-s-euw1-{resource_name}`

---

## 1. Small Smoke Test (15 articles, 3 tenants)

### Upload test data

5 articles per tenant. Upload to the source bucket under the correct path pattern:
`{prefix}/{tenant_id}/{region}/`

```bash
# tenantalpha
aws s3 sync data/test_multi_tenant/tenantalpha/ \
  s3://shared-s-euw1-curated-unstructured-s3-v3/tenant_partitioning/itsm/snow/kb_articles/tenantalpha/eu-west-1/ \
  --region eu-west-1

# tenantbeta
aws s3 sync data/test_multi_tenant/tenantbeta/ \
  s3://shared-s-euw1-curated-unstructured-s3-v3/tenant_partitioning/itsm/snow/kb_articles/tenantbeta/eu-west-1/ \
  --region eu-west-1

# tenantgamma
aws s3 sync data/test_multi_tenant/tenantgamma/ \
  s3://shared-s-euw1-curated-unstructured-s3-v3/tenant_partitioning/itsm/snow/kb_articles/tenantgamma/eu-west-1/ \
  --region eu-west-1
```

### Trigger

```bash
aws lambda invoke \
  --function-name shared-s-euw1-file_enumerator \
  --payload '{}' --region eu-west-1 /tmp/fe_output.json

cat /tmp/fe_output.json | python3 -m json.tool
```

Expected: `tenants_processed: 3, total_changed_files: 15, batches_sent: 3`

---

## 2. Full Load Test (1268 articles per tenant, 3 tenants)

### Test data

- `data/it_self_service_articles/` - 634 real articles converted from ServiceNow format
- `data/it_self_service_articles_synthetic/` - 634 synthetic articles generated via Bedrock Claude
- Total: 1268 articles per tenant, 3804 across 3 tenants

### Step 1: Upload articles to all 3 tenants

Copy the same 1268 articles to each tenant's S3 prefix. Each tenant gets its own copy so they have independent vector indexes and dedup runs.

```bash
SOURCE_BUCKET="shared-s-euw1-curated-unstructured-s3-v3"
PREFIX="tenant_partitioning/itsm/snow/kb_articles"
REGION="eu-west-1"

# Upload real articles (634 files) to tenantalpha
aws s3 sync data/it_self_service_articles/ \
  s3://${SOURCE_BUCKET}/${PREFIX}/tenantalpha/${REGION}/ \
  --region ${REGION}

# Upload synthetic articles (634 files) to tenantalpha
aws s3 sync data/it_self_service_articles_synthetic/ \
  s3://${SOURCE_BUCKET}/${PREFIX}/tenantalpha/${REGION}/ \
  --region ${REGION}

# Copy tenantalpha's data to tenantbeta (S3 server-side copy, no download)
aws s3 sync \
  s3://${SOURCE_BUCKET}/${PREFIX}/tenantalpha/${REGION}/ \
  s3://${SOURCE_BUCKET}/${PREFIX}/tenantbeta/${REGION}/ \
  --region ${REGION}

# Copy tenantalpha's data to tenantgamma
aws s3 sync \
  s3://${SOURCE_BUCKET}/${PREFIX}/tenantalpha/${REGION}/ \
  s3://${SOURCE_BUCKET}/${PREFIX}/tenantgamma/${REGION}/ \
  --region ${REGION}
```

Verify file counts:
```bash
for TENANT in tenantalpha tenantbeta tenantgamma; do
  COUNT=$(aws s3 ls s3://${SOURCE_BUCKET}/${PREFIX}/${TENANT}/${REGION}/ \
    --region ${REGION} --recursive | wc -l)
  echo "${TENANT}: ${COUNT} files"
done
```

Expected: 1268 files per tenant.

### Step 2: Clean previous run data

```bash
# Clean DynamoDB tables
python3 -c "
import boto3
ddb = boto3.resource('dynamodb', region_name='eu-west-1')
for tbl_name in ['shared-s-euw1-article_metadata', 'shared-s-euw1-pipeline_job_status']:
    t = ddb.Table(tbl_name)
    scan = t.scan()
    with t.batch_writer() as batch:
        for item in scan['Items']:
            key = {k['AttributeName']: item[k['AttributeName']] for k in t.key_schema}
            batch.delete_item(Key=key)
        while 'LastEvaluatedKey' in scan:
            scan = t.scan(ExclusiveStartKey=scan['LastEvaluatedKey'])
            for item in scan['Items']:
                key = {k['AttributeName']: item[k['AttributeName']] for k in t.key_schema}
                batch.delete_item(Key=key)
    print(f'Cleaned {tbl_name}')
"

# Clean pipeline bucket (raw + generated HTML from previous runs)
aws s3 rm s3://shared-s-euw1-kb-pipeline-v3/tenantalpha/ --recursive --region eu-west-1
aws s3 rm s3://shared-s-euw1-kb-pipeline-v3/tenantbeta/ --recursive --region eu-west-1
aws s3 rm s3://shared-s-euw1-kb-pipeline-v3/tenantgamma/ --recursive --region eu-west-1

# Purge SQS queue (in case leftover messages from previous run)
aws sqs purge-queue \
  --queue-url $(aws sqs get-queue-url --queue-name shared-s-euw1-kb_pipeline_batch_queue.fifo \
    --region eu-west-1 --query QueueUrl --output text) \
  --region eu-west-1
```

Note: S3 Vectors indexes are per-tenant and auto-created. If you need a clean dedup run, delete vectors manually:
```bash
# Only if you want to reset dedup state
python3 -c "
import boto3
s3v = boto3.client('s3vectors', region_name='eu-west-1')
for tenant in ['tenantalpha', 'tenantbeta', 'tenantgamma']:
    bucket_name = f'{tenant}-s-euw1-kb-vectors'
    index_name = f'{tenant}-s-euw1-kb-vector-index'
    try:
        resp = s3v.list_vectors(vectorBucketName=bucket_name, indexName=index_name)
        keys = [v['key'] for v in resp.get('vectors', [])]
        if keys:
            s3v.delete_vectors(vectorBucketName=bucket_name, indexName=index_name, keys=keys)
            print(f'{tenant}: deleted {len(keys)} vectors')
        else:
            print(f'{tenant}: no vectors to delete')
    except Exception as e:
        print(f'{tenant}: {e}')
"
```

### Step 3: Deploy with load test config

Make sure `base.yaml` has these values:

```yaml
pipeline:
  batch_size: 400
  max_workers_phase1: 8
  max_workers_phase2: 10

step_function:
  max_items_per_batch: 10
  max_concurrency: 40

change_detection:
  mode: s3_scan              # Use s3_scan for sandbox load testing

servicenow:
  review_limit: -1           # Disable ServiceNow calls during load test
```

Deploy:
```bash
cd infra
npx cdk deploy ArticleCuration-kbanalytics-s-euw1-shared --require-approval never
```

### Step 4: Trigger the pipeline

Option A - Set EventBridge schedule to fire in 2 minutes:
```yaml
schedule:
  cron_minute: "32"    # Set to current minute + 2
  cron_hour: "14"      # Current UTC hour
```
Deploy the change, then wait.

Option B - Direct Lambda invoke:
```bash
aws lambda invoke \
  --function-name shared-s-euw1-file_enumerator \
  --payload '{}' --region eu-west-1 /tmp/fe_output.json

cat /tmp/fe_output.json | python3 -m json.tool
```

Expected output:
```json
{
  "job_id": "KNOWLEDGE_CURATION_20260425T..._xxxxxxxx",
  "tenants_processed": 3,
  "total_changed_files": 3804,
  "batches_sent": 12
}
```

12 batches = 3 tenants x 4 batches each (1268 / 400 = 3 full + 1 remainder of 68).

### Step 5: Monitor progress

**Step Function executions:**
```bash
aws stepfunctions list-executions \
  --state-machine-arn $(aws stepfunctions list-state-machines --region eu-west-1 \
    --query "stateMachines[?contains(name,'article_curation_pipeline')].stateMachineArn" --output text) \
  --region eu-west-1 --max-results 15 \
  --query "executions[*].{name:name,status:status,start:startDate}" --output table
```

**SQS queue depth (messages waiting):**
```bash
aws sqs get-queue-attributes \
  --queue-url $(aws sqs get-queue-url --queue-name shared-s-euw1-kb_pipeline_batch_queue.fifo \
    --region eu-west-1 --query QueueUrl --output text) \
  --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible \
  --region eu-west-1
```

**DLQ check (should be 0):**
```bash
aws sqs get-queue-attributes \
  --queue-url $(aws sqs get-queue-url --queue-name shared-s-euw1-kb_pipeline_batch_queue_dlq.fifo \
    --region eu-west-1 --query QueueUrl --output text) \
  --attribute-names ApproximateNumberOfMessages \
  --region eu-west-1
```

**Article status breakdown:**
```bash
python3 -c "
import boto3
from collections import Counter
ddb = boto3.resource('dynamodb', region_name='eu-west-1')
table = ddb.Table('shared-s-euw1-article_metadata')
for tid in ['tenantalpha', 'tenantbeta', 'tenantgamma']:
    items = []
    resp = table.query(KeyConditionExpression=boto3.dynamodb.conditions.Key('tenant_code').eq(tid))
    items.extend(resp['Items'])
    while 'LastEvaluatedKey' in resp:
        resp = table.query(
            KeyConditionExpression=boto3.dynamodb.conditions.Key('tenant_code').eq(tid),
            ExclusiveStartKey=resp['LastEvaluatedKey'])
        items.extend(resp['Items'])
    statuses = Counter(item.get('pipeline_status', '?') for item in items)
    print(f'{tid}: {len(items)} articles -> {dict(statuses)}')
"
```

**Pipeline job status (group by parent_job_id):**
```bash
python3 -c "
import boto3
from collections import defaultdict
ddb = boto3.resource('dynamodb', region_name='eu-west-1')
table = ddb.Table('shared-s-euw1-pipeline_job_status')
scan = table.scan()
items = scan['Items']
while 'LastEvaluatedKey' in scan:
    scan = table.scan(ExclusiveStartKey=scan['LastEvaluatedKey'])
    items.extend(scan['Items'])

by_parent = defaultdict(list)
for item in items:
    pjid = item.get('parent_job_id', item.get('job_id', '?'))
    by_parent[pjid].append(item)

for pjid, batches in sorted(by_parent.items()):
    print(f'\nJob: {pjid}')
    for b in sorted(batches, key=lambda x: x.get('batch_id', '')):
        tenant = b.get('tenant_code', '?')
        status = b.get('Status', '?')
        batch = b.get('batch_id', '?')
        started = b.get('started_at', '?')[:19]
        completed = b.get('completed_at', '?')[:19] if b.get('completed_at') else 'running'
        print(f'  {tenant} | {batch} | {status} | {started} -> {completed}')
"
```

### Step 6: Capture performance metrics

After all 12 batches complete (all Step Function executions show SUCCEEDED):

```bash
python3 -c "
import boto3
from datetime import datetime
from collections import defaultdict

ddb = boto3.resource('dynamodb', region_name='eu-west-1')

# Job status
jt = ddb.Table('shared-s-euw1-pipeline_job_status')
scan = jt.scan()
jobs = scan['Items']
while 'LastEvaluatedKey' in scan:
    scan = jt.scan(ExclusiveStartKey=scan['LastEvaluatedKey'])
    jobs.extend(scan['Items'])

# Filter to latest parent_job_id
parent_ids = set(j.get('parent_job_id', '') for j in jobs if j.get('parent_job_id'))
latest_parent = sorted(parent_ids)[-1] if parent_ids else None
batches = [j for j in jobs if j.get('parent_job_id') == latest_parent]

print(f'Parent Job ID: {latest_parent}')
print(f'Total batches: {len(batches)}')
print(f'Completed: {sum(1 for b in batches if b.get(\"Status\") == \"COMPLETED\")}')
print(f'Failed: {sum(1 for b in batches if b.get(\"Status\") == \"FAILED\")}')
print(f'Running: {sum(1 for b in batches if b.get(\"Status\") == \"RUNNING\")}')

# Wall clock time
starts = [b['started_at'] for b in batches if b.get('started_at')]
ends = [b['completed_at'] for b in batches if b.get('completed_at')]
if starts and ends:
    first = min(starts)
    last = max(ends)
    t0 = datetime.fromisoformat(first.replace('Z', '+00:00'))
    t1 = datetime.fromisoformat(last.replace('Z', '+00:00'))
    wall_min = (t1 - t0).total_seconds() / 60
    print(f'Wall clock: {wall_min:.1f} min ({first[:19]} -> {last[:19]})')

# Article counts
at = ddb.Table('shared-s-euw1-article_metadata')
for tid in ['tenantalpha', 'tenantbeta', 'tenantgamma']:
    items = []
    resp = at.query(KeyConditionExpression=boto3.dynamodb.conditions.Key('tenant_code').eq(tid))
    items.extend(resp['Items'])
    while 'LastEvaluatedKey' in resp:
        resp = at.query(
            KeyConditionExpression=boto3.dynamodb.conditions.Key('tenant_code').eq(tid),
            ExclusiveStartKey=resp['LastEvaluatedKey'])
        items.extend(resp['Items'])
    from collections import Counter
    statuses = Counter(i.get('pipeline_status', '?') for i in items)
    terminal = sum(v for k, v in statuses.items() if k not in ('RAW', 'CLASSIFIED', 'EMBEDDED', 'UNIQUE'))
    print(f'{tid}: {len(items)} total, {terminal} terminal -> {dict(statuses)}')
"
```

---

## 3. Clean Up After Load Test

### Delete test data from source bucket

```bash
SOURCE_BUCKET="shared-s-euw1-curated-unstructured-s3-v3"
PREFIX="tenant_partitioning/itsm/snow/kb_articles"
REGION="eu-west-1"

aws s3 rm s3://${SOURCE_BUCKET}/${PREFIX}/tenantalpha/${REGION}/ --recursive --region ${REGION}
aws s3 rm s3://${SOURCE_BUCKET}/${PREFIX}/tenantbeta/${REGION}/ --recursive --region ${REGION}
aws s3 rm s3://${SOURCE_BUCKET}/${PREFIX}/tenantgamma/${REGION}/ --recursive --region ${REGION}
```

### Delete pipeline output

```bash
aws s3 rm s3://shared-s-euw1-kb-pipeline-v3/tenantalpha/ --recursive --region eu-west-1
aws s3 rm s3://shared-s-euw1-kb-pipeline-v3/tenantbeta/ --recursive --region eu-west-1
aws s3 rm s3://shared-s-euw1-kb-pipeline-v3/tenantgamma/ --recursive --region eu-west-1
```

### Clean DynamoDB

```bash
python3 -c "
import boto3
ddb = boto3.resource('dynamodb', region_name='eu-west-1')
for tbl_name in ['shared-s-euw1-article_metadata', 'shared-s-euw1-pipeline_job_status']:
    t = ddb.Table(tbl_name)
    scan = t.scan()
    with t.batch_writer() as batch:
        for item in scan['Items']:
            key = {k['AttributeName']: item[k['AttributeName']] for k in t.key_schema}
            batch.delete_item(Key=key)
        while 'LastEvaluatedKey' in scan:
            scan = t.scan(ExclusiveStartKey=scan['LastEvaluatedKey'])
            for item in scan['Items']:
                key = {k['AttributeName']: item[k['AttributeName']] for k in t.key_schema}
                batch.delete_item(Key=key)
    print(f'Cleaned {tbl_name}')
"
```

### Destroy CDK stack (optional, if done with sandbox)

```bash
cd infra
npx cdk destroy ArticleCuration-kbanalytics-s-euw1-shared --force
```

---

## Key Config Values

| Config | Location | Load Test Value | Purpose |
|--------|----------|----------------|---------|
| `pipeline.batch_size` | base.yaml | 400 | Articles per SQS message |
| `pipeline.max_workers_phase2` | base.yaml | 10 | Threads per Lambda for Bedrock calls |
| `step_function.max_items_per_batch` | base.yaml | 10 | Articles per distributed map child |
| `step_function.max_concurrency` | base.yaml | 40 | Parallel map children |
| `sqs.batch_queue.max_concurrency` | base.yaml | 10 | Parallel dispatcher Lambda instances |
| `change_detection.mode` | base.yaml | s3_scan | Use s3_scan for sandbox, glue_catalog for dev/prod |
| `servicenow.review_limit` | base.yaml | -1 | -1 disables SN calls during load test |

## Expected Results (3 tenants, 1268 articles each)

| Metric | Expected |
|--------|----------|
| Batches | 12 (4 per tenant) |
| Wall clock | ~17 min (parallel) |
| Throughput | ~226 articles/min |
| Cold start batches | 3 (one per tenant, run in parallel) |
| Terminal articles | 3801+ out of 3804 (99.9%) |
| DLQ messages | 0 |
| Failed executions | 0 |
