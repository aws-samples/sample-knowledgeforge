"""
File Enumerator Lambda - Main Handler

Triggered by EventBridge schedule or manual invocation. Discovers tenants, detects
changed articles, batches file references, and sends to SQS FIFO for pipeline processing.

Supports tenant filtering via event payload:
  - {"tenant_id": "orgAlpha"} - process single tenant
  - {"tenant_id": ["orgAlpha", "contoso"]} - process multiple tenants
  - {} - process all tenants (default)

Environment variables:
  SOURCE_BUCKET          - Data team's S3 bucket (read-only, contains KB articles)
  PIPELINE_BUCKET        - Pipeline bucket (raw HTML, manifests, generated output)
  CONTENT_GENERATOR_SOURCE_BUCKET - Problem Finder team's S3 bucket (IC/RCA articles)
  CONTENT_GENERATOR_TABLE   - Problem Finder DynamoDB table for change detection
  CONTENT_GENERATOR_GSI     - GSI name on Problem Finder table (tenant + generation_timestamp)
  QUEUE_URL              - SQS FIFO queue URL for batch messages
  BATCH_SIZE             - Number of changed file references per SQS message
  JOB_STATUS_TABLE       - DynamoDB pipeline_job_status table name
  ARTICLE_TABLE          - DynamoDB article_metadata table name
  GLUE_DATABASE          - Glue database name for Athena queries
  GLUE_TABLE             - Glue table name for Athena queries
  ATHENA_OUTPUT_LOCATION - S3 path for Athena query results
  APPCONFIG_SHARED_APP, APPCONFIG_SHARED_PROFILE, APPCONFIG_PROFILE,
  APPCONFIG_ENV, ENV_CODE, REGION_CODE - Standard AppConfig env vars
"""

import os
import json
import uuid
import boto3
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

from logger import get_logger
from org_discovery import discover_tenants
from org_config import load_tenant_context
from diff_engine import (
    get_last_pipeline_run,
    start_multi_tenant_athena_query,
    wait_and_fetch_multi_tenant_results,
    get_stuck_article_ids,
)
from record_writer import (
    load_json_articles_from_s3,
    ingest_articles_light,
)
from queue_sender import create_tenant_batches, send_batches_to_sqs
from helpers import tenant_s3_prefix

SOURCE_BUCKET = os.environ.get("SOURCE_BUCKET")
PIPELINE_BUCKET = os.environ.get("PIPELINE_BUCKET")
CONTENT_GENERATOR_SOURCE_BUCKET = os.environ.get("CONTENT_GENERATOR_SOURCE_BUCKET", "")
CONTENT_GENERATOR_TABLE = os.environ.get("CONTENT_GENERATOR_TABLE", "")
CONTENT_GENERATOR_GSI = os.environ.get("CONTENT_GENERATOR_GSI", "")
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "50"))
REGION = os.environ.get("AWS_REGION", "")
GLUE_DATABASE = os.environ.get("GLUE_DATABASE", "")
GLUE_TABLE = os.environ.get("GLUE_TABLE", "")
ATHENA_OUTPUT_LOCATION = os.environ.get("ATHENA_OUTPUT_LOCATION", "")

# Validate required environment variables
if not SOURCE_BUCKET:
    raise ValueError("SOURCE_BUCKET environment variable is required")
if not PIPELINE_BUCKET:
    raise ValueError("PIPELINE_BUCKET environment variable is required")

s3 = boto3.client("s3", region_name=REGION)
dynamodb = boto3.resource("dynamodb", region_name=REGION)

log = get_logger(lambda_name="file_enumerator")

# Problem Finder resource existence check (cached for Lambda lifetime)
_content_generator_resources_exist = None  # None = not checked yet


def _check_content_generator_resources():
    """Check if Problem Finder AWS resources actually exist.
    Cached after first call for the lifetime of the Lambda instance.
    Returns True only if BOTH the S3 bucket and DynamoDB table exist.
    """
    global _content_generator_resources_exist
    if _content_generator_resources_exist is not None:
        return _content_generator_resources_exist

    if not CONTENT_GENERATOR_TABLE or not CONTENT_GENERATOR_GSI or not CONTENT_GENERATOR_SOURCE_BUCKET:
        _content_generator_resources_exist = False
        return False

    # Check DynamoDB table exists
    try:
        dynamodb.meta.client.describe_table(TableName=CONTENT_GENERATOR_TABLE)
    except dynamodb.meta.client.exceptions.ResourceNotFoundException:
        log.info(
            "Problem Finder DynamoDB table does not exist, skipping",
            table=CONTENT_GENERATOR_TABLE,
        )
        _content_generator_resources_exist = False
        return False
    except Exception as e:
        log.warn(
            "Problem Finder DynamoDB table check failed, skipping",
            table=CONTENT_GENERATOR_TABLE,
            error=str(e),
        )
        _content_generator_resources_exist = False
        return False

    # Check S3 bucket exists
    try:
        s3.head_bucket(Bucket=CONTENT_GENERATOR_SOURCE_BUCKET)
    except Exception as e:
        error_code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
        if error_code in ("404", "NoSuchBucket"):
            log.info(
                "Problem Finder S3 bucket does not exist, skipping",
                bucket=CONTENT_GENERATOR_SOURCE_BUCKET,
            )
        else:
            log.warn(
                "Problem Finder S3 bucket check failed, skipping",
                bucket=CONTENT_GENERATOR_SOURCE_BUCKET,
                error=str(e),
            )
        _content_generator_resources_exist = False
        return False

    log.info(
        "Problem Finder resources verified",
        table=CONTENT_GENERATOR_TABLE,
        bucket=CONTENT_GENERATOR_SOURCE_BUCKET,
    )
    _content_generator_resources_exist = True
    return True

# Module-level: tenant mapping data (populated in lambda_handler from AppConfig)
TENANT_CODE_TO_TENANT_ID = {}  # {tenant_code: tenant_uuid}
TENANT_CODE_TO_HOME_REGION = {}  # {tenant_code: home_region}


def _query_content_generator_articles(tenant_id, job_id, tenant_uuid=None):
    """Query Problem Finder DynamoDB table for new articles using GSI.

    Supports incremental (only new since last run) and full (all articles) modes.
    Uses the tenant-generation-timestamp-index GSI for efficient querying.

    Args:
        tenant_id: Short tenant code (e.g. 'orgAlpha') — used for Problem Finder GSI query
        job_id: Pipeline job ID
        tenant_uuid: UUID for DynamoDB PK in pipeline_job_status table

    Returns a list of article dicts ready for ingest_articles_light.
    """
    from boto3.dynamodb.conditions import Key, Attr

    pf_table = dynamodb.Table(CONTENT_GENERATOR_TABLE)
    job_status_table = dynamodb.Table(os.environ.get("JOB_STATUS_TABLE", ""))
    pk_value = tenant_uuid or tenant_id  # UUID for DynamoDB PK

    # Get last processed timestamp for incremental detection
    last_timestamp = None
    try:
        resp = job_status_table.get_item(
            Key={
                "tenant_id": pk_value,
                "job_id": f"{tenant_id}_CONTENT_GENERATOR_LAST_TIMESTAMP",
            }
        )
        item = resp.get("Item")
        if item:
            last_timestamp = item.get("last_generation_timestamp", "")
    except Exception as e:
        log.warn(
            "Failed to get last Problem Finder timestamp, doing full load",
            tenant_id=tenant_id,
            error=str(e),
        )

    # Query GSI: tenant + generation_timestamp
    query_kwargs = {
        "IndexName": CONTENT_GENERATOR_GSI,
        "KeyConditionExpression": Key("tenant").eq(tenant_id),
    }

    if last_timestamp:
        query_kwargs["KeyConditionExpression"] = (
            Key("tenant").eq(tenant_id)
            & Key("generation_timestamp").gt(last_timestamp)
        )
        log.info(
            "Problem Finder incremental query",
            tenant_id=tenant_id,
            since=last_timestamp,
        )
    else:
        log.info("Problem Finder full load", tenant_id=tenant_id)

    # Filter to only DOC# records (skip RUN records)
    query_kwargs["FilterExpression"] = Attr("record_type").begins_with("DOC#")

    # Paginate through all results
    doc_records = []
    while True:
        resp = pf_table.query(**query_kwargs)
        doc_records.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        query_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    if not doc_records:
        log.info("No new Problem Finder articles", tenant_id=tenant_id)
        return []

    log.info(
        "Problem Finder articles found",
        tenant_id=tenant_id,
        count=len(doc_records),
        mode="incremental" if last_timestamp else "full",
    )

    # Convert DOC records to article dicts for ingestion
    articles = []
    latest_timestamp = last_timestamp or ""

    for doc in doc_records:
        s3_key = doc.get("s3_key", "")
        article_uuid = doc.get("article_uuid", "")
        article_type = doc.get("article_type", "KB")  # KB (IC) or RCA
        gen_ts = doc.get("generation_timestamp", "")

        if not s3_key or not article_uuid:
            continue

        # Determine source_system from article_type
        source_system = (
            "CONTENT_GENERATOR_RCA" if article_type == "RCA" else "CONTENT_GENERATOR_IC"
        )

        # Read the actual article JSON from S3
        try:
            obj = s3.get_object(Bucket=CONTENT_GENERATOR_SOURCE_BUCKET, Key=s3_key)
            article_data = json.loads(obj["Body"].read().decode("utf-8"))
        except Exception as e:
            log.warn(
                "Failed to read Problem Finder article from S3",
                s3_key=s3_key,
                error=str(e),
            )
            continue

        # Ensure required fields
        if isinstance(article_data, dict):
            article_data["src_kb_article_id"] = article_uuid
            article_data["source_system"] = source_system
            article_data["_source_s3_key"] = s3_key
            article_data["_source_s3_bucket"] = CONTENT_GENERATOR_SOURCE_BUCKET
            if "short_description" not in article_data:
                article_data["short_description"] = doc.get("short_description", "")
            articles.append(article_data)

        # Track latest timestamp for incremental bookmark
        if gen_ts > latest_timestamp:
            latest_timestamp = gen_ts

    # Store the latest timestamp for next incremental run
    if latest_timestamp and job_status_table.table_name:
        try:
            import time

            ttl_timestamp = int(time.time()) + (90 * 24 * 60 * 60)  # 90 days
            job_status_table.put_item(
                Item={
                    "tenant_id": pk_value,
                    "tenant_code": tenant_id,
                    "home_region": TENANT_CODE_TO_HOME_REGION.get(tenant_id, ""),
                    "job_id": f"{tenant_id}_CONTENT_GENERATOR_LAST_TIMESTAMP",
                    "last_generation_timestamp": latest_timestamp,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "ttl": ttl_timestamp,
                }
            )
        except Exception as e:
            log.warn(
                "Failed to store Problem Finder timestamp bookmark",
                tenant_id=tenant_id,
                error=str(e),
            )

    return articles


def process_all_tenants_parallel(tenants, job_id):
    """Process all tenants with ONE Athena query for ITSM_KB and S3 scans for Problem Finder.

    OPTIMIZED: Single Athena query for all tenants instead of N separate queries.
    Fetches metadata from Athena (no S3 reads for ITSM_KB articles).

    1. Get last pipeline run timestamp for each tenant
    2. Fire ONE Athena query for all tenants with their respective timestamps
    3. Wait for query to complete and group results by tenant
    4. Process each tenant in parallel (DynamoDB ingestion + Problem Finder scan)
    """
    athena_enabled = bool(GLUE_DATABASE and GLUE_TABLE and ATHENA_OUTPUT_LOCATION)
    if not athena_enabled:
        log.info("Glue/Athena not configured, skipping ITSM_KB change detection")

    # Step 1: Get last pipeline run for all tenants (parallel)
    tenant_timestamps = {}
    if athena_enabled:
        timestamp_start = datetime.now(timezone.utc)
        log.info(
            "Fetching last pipeline run timestamps for all tenants",
            tenant_count=len(tenants),
        )

        def _get_timestamp(tenant_id):
            # Use UUID for DynamoDB PK query
            uuid_val = TENANT_CODE_TO_TENANT_ID.get(tenant_id, tenant_id)
            return tenant_id, get_last_pipeline_run(uuid_val)

        workers = min(20, len(tenants))
        if workers > 0:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [executor.submit(_get_timestamp, tid) for tid in tenants]
                for future in as_completed(futures):
                    tenant_id, last_run = future.result()
                    tenant_timestamps[tenant_id] = last_run

        timestamp_elapsed = (
            datetime.now(timezone.utc) - timestamp_start
        ).total_seconds()
        log.info(
            "Last pipeline run timestamps fetched",
            tenant_count=len(tenant_timestamps),
            incremental_count=sum(1 for ts in tenant_timestamps.values() if ts),
            full_load_count=sum(1 for ts in tenant_timestamps.values() if not ts),
            elapsed_seconds=round(timestamp_elapsed, 2),
        )

    # Step 2: Fire ONE Athena query for all tenants
    articles_by_tenant = {}
    if athena_enabled and tenant_timestamps:
        athena_start = datetime.now(timezone.utc)
        log.info(
            "Starting single Athena query for all tenants (OPTIMIZED)",
            tenant_count=len(tenant_timestamps),
        )

        # Convert tenant_codes to UUIDs for Athena query (Glue table uses UUIDs)
        if TENANT_CODE_TO_TENANT_ID:
            athena_timestamps = {
                TENANT_CODE_TO_TENANT_ID[code]: ts
                for code, ts in tenant_timestamps.items()
                if code in TENANT_CODE_TO_TENANT_ID
            }
        else:
            athena_timestamps = tenant_timestamps

        query_id = start_multi_tenant_athena_query(athena_timestamps)
        if query_id:
            # Wait and fetch results grouped by tenant (keys are UUIDs from Glue)
            raw_articles_by_tenant = wait_and_fetch_multi_tenant_results(
                query_id, max_wait_seconds=300
            )

            # Remap UUID keys back to tenant_codes for downstream processing
            if TENANT_CODE_TO_TENANT_ID:
                uuid_to_code = {
                    uuid: code for code, uuid in TENANT_CODE_TO_TENANT_ID.items()
                }
                articles_by_tenant = {}
                for uuid_key, arts in raw_articles_by_tenant.items():
                    code = uuid_to_code.get(uuid_key, uuid_key)
                    articles_by_tenant[code] = arts
            else:
                articles_by_tenant = raw_articles_by_tenant

            athena_elapsed = (datetime.now(timezone.utc) - athena_start).total_seconds()

            total_articles = sum(len(arts) for arts in articles_by_tenant.values())
            log.info(
                "Single Athena query completed",
                tenant_count=len(articles_by_tenant),
                total_articles=total_articles,
                elapsed_seconds=round(athena_elapsed, 2),
                articles_per_tenant={
                    tid: len(arts)
                    for tid, arts in list(articles_by_tenant.items())[:10]
                },
            )
        else:
            log.error("Failed to start multi-tenant Athena query")

    # Step 3: Process each tenant in parallel
    processing_start = datetime.now(timezone.utc)
    all_changed_files = []
    now = datetime.now(timezone.utc).isoformat()

    log.info(
        "Starting tenant processing phase",
        tenant_count=len(tenants),
        athena_enabled=athena_enabled,
        content_generator_enabled=_check_content_generator_resources(),
    )

    def _process_one_tenant(tenant_id):
        # tenant_id here is the short code (e.g. 'orgAlpha')
        # tenant_uuid is the UUID used as DynamoDB PK
        tenant_uuid = TENANT_CODE_TO_TENANT_ID.get(tenant_id, tenant_id)

        try:
            ctx = load_tenant_context(
                tenant_id,
                home_region=TENANT_CODE_TO_HOME_REGION.get(tenant_id, ""),
            )
        except Exception as e:
            log.error(
                "AppConfig load failed, skipping tenant",
                tenant_id=tenant_id,
                error=str(e),
            )
            return []

        table = dynamodb.Table(ctx.table_name)
        articles = articles_by_tenant.get(tenant_id, [])
        changed_files = []
        stuck_articles = []

        # Always check for stuck articles (RAW, CLASSIFIED, EMBEDDED, ERROR_S3_FILE_MISSING)
        # These need retry regardless of whether Athena found new changes
        if athena_enabled:
            stuck_articles = get_stuck_article_ids(table, tenant_uuid)

        # ITSM_KB: Process articles from Athena (NO S3 READS - already have all data!)
        if athena_enabled and articles:
            # Build exclusion set: articles already in DynamoDB that need retry
            # This prevents processing the same article twice
            excluded_ids = {a["article_id"] for a in stuck_articles}

            # Filter out articles that are already being retried
            new_articles = [
                a for a in articles if a["src_kb_article_id"] not in excluded_ids
            ]

            if new_articles:
                log.info(
                    "Processing articles from Athena (OPTIMIZED - no S3 reads)",
                    tenant_id=tenant_id,
                    count=len(new_articles),
                    excluded_for_retry=len(excluded_ids),
                )
                to_process, _ = ingest_articles_light(
                    table,
                    now,
                    new_articles,
                    "ITSM_KB",
                    ctx=ctx,
                    dynamodb_resource=dynamodb,
                    home_region=TENANT_CODE_TO_HOME_REGION.get(tenant_id, ""),
                    tenant_uuid=TENANT_CODE_TO_TENANT_ID.get(tenant_id, ""),
                )
                for item in to_process:
                    changed_files.append(
                        {
                            "tenant_code": tenant_id,
                            "tenant_id": TENANT_CODE_TO_TENANT_ID.get(
                                tenant_id, tenant_id
                            ),
                            "home_region": TENANT_CODE_TO_HOME_REGION.get(
                                tenant_id, ""
                            ),
                            "article_id": item["article_id"],
                            "source_file_path": item["source_file_path"],
                            "reason": item["reason"],
                        }
                    )

        # Problem Finder: query DynamoDB GSI for new articles
        if _check_content_generator_resources():
            try:
                pf_articles = _query_content_generator_articles(tenant_id, job_id, tenant_uuid=tenant_uuid)
                if pf_articles:
                    # Group by source_system (IC vs RCA) for proper ingestion
                    pf_by_system = {}
                    for art in pf_articles:
                        ss = art.get("source_system", "CONTENT_GENERATOR_IC")
                        pf_by_system.setdefault(ss, []).append(art)

                    for source_system, arts in pf_by_system.items():
                        pf_processed, _ = ingest_articles_light(
                            table,
                            now,
                            arts,
                            source_system,
                            ctx=ctx,
                            dynamodb_resource=dynamodb,
                            home_region=TENANT_CODE_TO_HOME_REGION.get(tenant_id, ""),
                            tenant_uuid=TENANT_CODE_TO_TENANT_ID.get(tenant_id, ""),
                        )
                        for item in pf_processed:
                            changed_files.append(
                                {
                                    "tenant_code": tenant_id,
                                    "tenant_id": TENANT_CODE_TO_TENANT_ID.get(
                                        tenant_id, tenant_id
                                    ),
                                    "home_region": TENANT_CODE_TO_HOME_REGION.get(
                                        tenant_id, ""
                                    ),
                                    "article_id": item["article_id"],
                                    "source_file_path": item["source_file_path"],
                                    "reason": item["reason"],
                                }
                            )
            except Exception as e:
                log.warn(
                    "Problem Finder DynamoDB query failed",
                    tenant_id=tenant_id,
                    error=str(e),
                )

        # Stuck articles for retry (includes ERROR_S3_FILE_MISSING for rare missing file cases)
        for stuck in stuck_articles:
            changed_files.append(
                {
                    "tenant_code": tenant_id,
                    "tenant_id": TENANT_CODE_TO_TENANT_ID.get(tenant_id, tenant_id),
                    "home_region": TENANT_CODE_TO_HOME_REGION.get(tenant_id, ""),
                    "article_id": stuck["article_id"],
                    "source_file_path": stuck["source_file_path"],
                    "reason": stuck["reason"],
                }
            )

        log.info(
            "Tenant processed",
            tenant_id=tenant_id,
            changed=len(changed_files),
            stuck=len(stuck_articles),
        )
        return changed_files

    # Process tenants in parallel (thread-safe via TenantContext)
    workers = min(20, len(tenants))
    log.info(
        "Processing tenants in parallel (OPTIMIZED)",
        count=len(tenants),
        workers=workers,
    )

    if not tenants:
        log.info("No tenants discovered, skipping processing")
        return all_changed_files

    completed_count = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_process_one_tenant, tid): tid for tid in tenants}
        for future in as_completed(futures):
            tid = futures[future]
            try:
                changed = future.result()
                all_changed_files.extend(changed)
                completed_count += 1

                # Log progress every 10 tenants
                if completed_count % 10 == 0:
                    log.info(
                        "Tenant processing progress",
                        completed=completed_count,
                        total=len(tenants),
                        percent=round(completed_count / len(tenants) * 100, 1),
                    )
            except Exception as e:
                log.error("Tenant processing failed", tenant_id=tid, error=str(e))

    processing_elapsed = (datetime.now(timezone.utc) - processing_start).total_seconds()
    log.info(
        "Tenant processing phase completed",
        tenants_processed=len(tenants),
        total_changed_files=len(all_changed_files),
        elapsed_seconds=round(processing_elapsed, 2),
    )

    # Final deduplication: ensure no article is processed twice
    # This is a safety check in case the same article appears in multiple sources
    seen_articles = {}
    deduplicated_files = []
    duplicates_removed = 0

    for file_ref in all_changed_files:
        article_key = (file_ref["tenant_id"], file_ref["article_id"])
        if article_key not in seen_articles:
            seen_articles[article_key] = file_ref
            deduplicated_files.append(file_ref)
        else:
            duplicates_removed += 1
            log.warn(
                "Duplicate article detected and removed",
                tenant_code=file_ref["tenant_id"],
                article_id=file_ref["article_id"],
                reason_kept=seen_articles[article_key]["reason"],
                reason_removed=file_ref["reason"],
            )

    if duplicates_removed > 0:
        log.warn(
            "Removed duplicate articles before SQS send",
            duplicates_removed=duplicates_removed,
            original_count=len(all_changed_files),
            deduplicated_count=len(deduplicated_files),
        )

    return deduplicated_files


def lambda_handler(event, context):
    """Entry point invoked by EventBridge schedule or manual invocation.

    Supports tenant filtering via event payload:
      - {"tenant_id": "orgAlpha"} - process single tenant
      - {"tenant_id": ["orgAlpha", "contoso"]} - process multiple tenants
      - {} - process all tenants (default)

    1. Discovers all tenant prefixes from Glue catalog
    2. Filters to requested tenant(s) if specified
    3. Runs change detection for each tenant
    4. Batches changed files and sends to SQS FIFO
    5. Returns summary stats
    """
    log.info(
        "File enumerator started",
        source_bucket=SOURCE_BUCKET,
        content_generator_source_bucket=CONTENT_GENERATOR_SOURCE_BUCKET,
        pipeline_bucket=PIPELINE_BUCKET,
        batch_size=BATCH_SIZE,
    )

    handler_start = datetime.now(timezone.utc)

    # Discover all tenants from Glue catalog
    # Returns list of dicts: [{"tenant_id": UUID, "tenant_code": str, "aws_region": str}]
    discovered_tenants = discover_tenants()
    discovery_elapsed = (datetime.now(timezone.utc) - handler_start).total_seconds()
    log.info(
        "Tenant discovery complete",
        tenant_count=len(discovered_tenants),
        elapsed_seconds=round(discovery_elapsed, 1),
    )

    # Build tenant mappings directly from Glue columns (tenant_code, aws_region)
    # No static config mapping needed — Glue is the source of truth.
    global TENANT_CODE_TO_TENANT_ID, TENANT_CODE_TO_HOME_REGION
    mapped_tenants = {}  # {tenant_code: tenant_uuid}
    home_regions = {}  # {tenant_code: aws_region}
    skipped = []

    for t in discovered_tenants:
        tenant_uuid = t.get("tenant_id", "")
        tenant_code = t.get("tenant_code", "")
        aws_region = t.get("aws_region", "")

        if not tenant_code or not aws_region:
            skipped.append(tenant_uuid)
            log.warn(
                "Tenant skipped - missing tenant_code or aws_region in Glue",
                tenant_id=tenant_uuid,
                tenant_code=tenant_code,
                aws_region=aws_region,
            )
            continue

        mapped_tenants[tenant_code] = tenant_uuid
        home_regions[tenant_code] = aws_region
        log.info(
            "Tenant mapped from Glue",
            tenant_id=tenant_uuid,
            tenant_code=tenant_code,
            aws_region=aws_region,
        )

    if skipped:
        log.warn(
            "Tenants skipped (missing tenant_code/aws_region in Glue)",
            skipped_count=len(skipped),
            skipped_ids=skipped,
        )

    all_tenants = list(mapped_tenants.keys())
    TENANT_CODE_TO_TENANT_ID = mapped_tenants
    TENANT_CODE_TO_HOME_REGION = home_regions

    log.info(
        "Tenant mapping from Glue applied",
        mapped_count=len(all_tenants),
        skipped_count=len(skipped),
        final_tenants=all_tenants,
        code_to_id_mapping=TENANT_CODE_TO_TENANT_ID,
        code_to_home_region=TENANT_CODE_TO_HOME_REGION,
    )

    # Parse tenant filter from event (supports string or list)
    tenant_filter = event.get("tenant_id")
    tenants = all_tenants  # Default: process all tenants

    if tenant_filter:
        # Normalize to list for consistent handling
        if isinstance(tenant_filter, str):
            requested_tenants = [tenant_filter]
            filter_mode = "single"
        elif isinstance(tenant_filter, list):
            requested_tenants = tenant_filter
            filter_mode = "multiple"
        else:
            log.error(
                "Invalid tenant_id format - must be string or list",
                tenant_id_type=type(tenant_filter).__name__,
            )
            return {
                "error": "INVALID_TENANT_ID_FORMAT",
                "message": "tenant_id must be a string or list of strings",
                "received_type": type(tenant_filter).__name__,
            }

        # Validate all requested tenants exist in Glue catalog
        invalid_tenants = [t for t in requested_tenants if t not in all_tenants]
        valid_tenants = [t for t in requested_tenants if t in all_tenants]

        if invalid_tenants:
            log.error(
                "Requested tenant(s) not found in Glue catalog",
                invalid_tenants=invalid_tenants,
                valid_tenants=valid_tenants,
                available_tenants=all_tenants,
            )
            return {
                "error": "TENANT_NOT_FOUND",
                "invalid_tenants": invalid_tenants,
                "valid_tenants": valid_tenants,
                "available_tenants": all_tenants,
            }

        # Filter to requested tenants
        tenants = valid_tenants
        log.info(
            "Filtering to requested tenant(s)",
            mode=filter_mode,
            requested_count=len(requested_tenants),
            filtered_tenants=tenants,
            total_available=len(all_tenants),
        )
    else:
        log.info(
            "No tenant filter specified, processing all tenants",
            tenant_count=len(tenants),
        )

    # discovery_only mode: just discover tenants and count S3 files, no AppConfig/DynamoDB/SQS
    discovery_only = event.get("discovery_only", False)
    if discovery_only:
        file_counts = {}

        def _count_files(tenant_id):
            prefix = tenant_s3_prefix(tenant_id)
            count = 0
            paginator = s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=SOURCE_BUCKET, Prefix=prefix):
                count += sum(
                    1
                    for obj in page.get("Contents", [])
                    if obj["Key"].lower().endswith(".json")
                )
            return tenant_id, count

        workers = min(64, len(tenants))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(_count_files, tid) for tid in tenants]
            for future in as_completed(futures):
                tid, count = future.result()
                file_counts[tid] = count

        total_elapsed = (datetime.now(timezone.utc) - handler_start).total_seconds()
        total_files = sum(file_counts.values())
        log.info(
            "Discovery only complete",
            tenants=len(tenants),
            total_files=total_files,
            discovery_seconds=round(discovery_elapsed, 1),
            total_elapsed_seconds=round(total_elapsed, 1),
        )
        return {
            "discovery_only": True,
            "tenants_discovered": len(tenants),
            "total_files": total_files,
            "files_per_tenant_sample": dict(list(file_counts.items())[:5]),
            "discovery_seconds": round(discovery_elapsed, 1),
            "total_elapsed_seconds": round(total_elapsed, 1),
        }

    # Generate job_id upfront so it flows into article_metadata during change detection
    job_id = (
        f"KNOWLEDGE_CURATION_"
        f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}_"
        f"{uuid.uuid4().hex[:8]}"
    )

    all_changed_files = process_all_tenants_parallel(tenants, job_id)

    total_changed = len(all_changed_files)

    if total_changed == 0:
        log.info(
            "No changes detected across all tenants, no SQS messages sent",
            tenant_count=len(tenants),
        )
        return {
            "tenants_processed": len(tenants),
            "total_changed_files": 0,
            "batches_sent": 0,
        }

    batches = create_tenant_batches(all_changed_files, BATCH_SIZE)

    # dry_run mode: skip SQS sending, only measure discovery + processing time
    dry_run = event.get("dry_run", False)
    if dry_run:
        total_elapsed = (datetime.now(timezone.utc) - handler_start).total_seconds()
        log.info(
            "File enumerator complete (dry_run — SQS skipped)",
            job_id=job_id,
            tenants_processed=len(tenants),
            total_changed_files=total_changed,
            total_batches=len(batches),
            total_elapsed_seconds=round(total_elapsed, 1),
        )
        return {
            "job_id": job_id,
            "tenants_processed": len(tenants),
            "total_changed_files": total_changed,
            "batches_sent": 0,
            "dry_run": True,
            "total_elapsed_seconds": round(total_elapsed, 1),
        }

    sent_count = send_batches_to_sqs(batches, job_id)

    total_elapsed = (datetime.now(timezone.utc) - handler_start).total_seconds()
    log.info(
        "File enumerator complete",
        job_id=job_id,
        tenants_processed=len(tenants),
        total_changed_files=total_changed,
        total_batches=len(batches),
        batches_sent=sent_count,
        total_elapsed_seconds=round(total_elapsed, 1),
    )

    return {
        "job_id": job_id,
        "tenants_processed": len(tenants),
        "total_changed_files": total_changed,
        "batches_sent": sent_count,
        "total_elapsed_seconds": round(total_elapsed, 1),
    }
