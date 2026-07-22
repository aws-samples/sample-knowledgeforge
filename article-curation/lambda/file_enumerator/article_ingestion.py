"""
Article Ingestion Module

Handles loading articles from S3 and writing to DynamoDB.
Includes JSON parsing, metadata extraction, and light ingestion.
"""

import os
import json
import boto3
from concurrent.futures import ThreadPoolExecutor, as_completed
from logger import get_logger
from utils import tenant_s3_prefix

SOURCE_BUCKET = os.environ.get("SOURCE_BUCKET", "")
REGION = os.environ.get("AWS_REGION", "")

s3 = boto3.client("s3", region_name=REGION)
s3vectors = boto3.client("s3vectors", region_name=REGION)
log = get_logger(lambda_name="file_enumerator")


def extract_metadata(article, source_system="ITSM_KB", cached_language=None):
    """Extract metadata fields for DynamoDB storage.
    Uses data warehouse column names.

    RELEASE 1: All articles are English - language detection disabled.
    Language detection via Comprehend is commented out for Release 1.
    Will be re-enabled in future releases when multi-language support is needed.

    If cached_language is provided (from an existing DynamoDB record), uses it.
    """
    # RELEASE 1: Assume all articles are English
    language = cached_language or article.get("language", "en")

    # COMMENTED OUT FOR RELEASE 1: Language detection via Comprehend
    # Will be re-enabled when multi-language support is needed
    # if not language and source_system == 'ITSM_KB':
    #     html_content = extract_text_field(article)
    #     from html_utils import extract_plain_text
    #     plain = extract_plain_text(html_content)
    #     language = detect_language(plain)
    # elif not language:
    #     language = 'en'

    return {
        "src_kb_article_id": article.get("src_kb_article_id", ""),
        "article_title": article.get("article_title", ""),
        "kb_category": article.get("kb_category", ""),
        "last_updated_ts_utc": article.get("last_updated_ts_utc", ""),
        "created_ts_utc": article.get("created_ts_utc", ""),
        "kb_author": article.get("kb_author", ""),
        "language": language,
        "kb_valid_to_ts": article.get("kb_valid_to_ts", ""),
        "sys_class_name": article.get("sys_class_name", ""),
        "sys_domain": article.get("sys_domain", ""),
        "description": article.get("description", ""),
        "active": article.get("active", ""),
        "status": article.get("status", ""),
        "can_read_user_criteria": article.get("can_read_user_criteria", ""),
    }


def delete_vector(article_id, ctx):
    """Delete a vector from S3 Vectors if it exists.
    Needed because classify_embed skips re-embedding if a vector already exists.
    """
    try:
        s3vectors.delete_vectors(
            vectorBucketName=ctx.vector_bucket,
            indexName=ctx.vector_index,
            keys=[article_id],
        )
        log.info("Vector deleted", article_id=article_id)
    except Exception as e:
        log.warn("Vector delete failed", article_id=article_id, error=str(e))


def parse_json_payload(data, source_key=""):
    """Parse a JSON payload into a flat list of article dicts.
    Supports:
      - Single article object: {"sys_id": ..., "text": ...}
      - Array of articles: [{"sys_id": ...}, ...]
      - ServiceNow result wrapper: {"result": {"data": [...]}}
      - Simple result array: {"result": [...]}
      - Records wrapper: {"records": [...]}
    """
    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        if "records" in data and isinstance(data["records"], list):
            return data["records"]
        if "result" in data:
            result = data["result"]
            if isinstance(result, list):
                return result
            if isinstance(result, dict) and "data" in result:
                return result["data"]
        if "src_kb_article_id" in data:
            return [data]

    log.warn(
        "Unrecognized JSON structure", source_key=source_key, type=type(data).__name__
    )
    return []


def load_json_articles_from_s3(bucket, prefix):
    """List and load all JSON files from a given S3 prefix using parallel reads.
    Returns a flat list of article dicts.
    """
    keys = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            if obj["Key"].lower().endswith(".json"):
                keys.append(obj["Key"])

    if not keys:
        log.info("No JSON files found", prefix=prefix)
        return []

    log.info("Found JSON files", prefix=prefix, count=len(keys))

    articles = []

    def _read_one(key):
        resp = s3.get_object(Bucket=bucket, Key=key)
        data = json.loads(resp["Body"].read().decode("utf-8"))
        parsed = parse_json_payload(data, key)
        for article in parsed:
            article["_source_s3_key"] = key
            article["_source_s3_bucket"] = bucket
        return parsed

    workers = min(32, len(keys))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_read_one, k): k for k in keys}
        for future in as_completed(futures):
            key = futures[future]
            try:
                articles.extend(future.result())
            except Exception as e:
                log.error("Failed to load JSON file", s3_key=key, error=str(e))

    log.info("JSON ingestion complete", prefix=prefix, total_articles=len(articles))
    return articles


def ingest_articles_light(
    table,
    now,
    articles,
    source_system="ITSM_KB",
    ctx=None,
    dynamodb_resource=None,
    home_region=None,
    tenant_uuid=None,
):
    """Light ingestion. Validates articles, detects language, writes DynamoDB.
    NO S3 upload - downstream lambdas read directly from source S3.
    ctx (TenantContext) is required for thread-safe parallel processing.
    dynamodb_resource is required for batch_get_item operations.
    """
    if not ctx:
        raise ValueError("TenantContext (ctx) is required for ingest_articles_light")
    if not dynamodb_resource:
        raise ValueError("dynamodb_resource is required for ingest_articles_light")

    tid = ctx.tenant_id  # short code (e.g. 'acme') — used for S3 paths
    tenant_id_value = tenant_uuid or tid  # UUID for DynamoDB PK (falls back to tid if no UUID)
    pstatuses = ctx.protected_statuses
    to_process = []
    stats = {
        "articles_received": len(articles),
        "ingested": 0,
        "language_filtered": 0,
        "skipped_no_content": 0,
        "language_breakdown": {},
    }

    # Batch read existing articles from DynamoDB upfront (optimization)
    raw_article_ids = [
        a.get("src_kb_article_id") for a in articles if a.get("src_kb_article_id")
    ]
    # Deduplicate article_ids (Glue may return multiple rows for same article)
    article_ids = list(dict.fromkeys(raw_article_ids))
    duplicates_removed = len(raw_article_ids) - len(article_ids)
    if duplicates_removed > 0:
        log.warn(
            "Duplicate article_ids removed from Glue results",
            tenant_id=tid,
            original_count=len(raw_article_ids),
            deduplicated_count=len(article_ids),
            duplicates_removed=duplicates_removed,
        )
    existing_articles = {}

    if article_ids:
        log.info(
            "Batch reading existing articles from DynamoDB",
            tenant_id=tid,
            count=len(article_ids),
        )

        # Batch read in chunks of 100 (DynamoDB limit)
        for i in range(0, len(article_ids), 100):
            batch_keys = [
                {"tenant_id": tenant_id_value, "src_kb_article_id": aid}
                for aid in article_ids[i : i + 100]
            ]
            try:
                response = dynamodb_resource.batch_get_item(
                    RequestItems={
                        table.name: {
                            "Keys": batch_keys,
                            "ProjectionExpression": "src_kb_article_id, pipeline_status, #lang",
                            "ExpressionAttributeNames": {"#lang": "language"},
                        }
                    }
                )
                for item in response.get("Responses", {}).get(table.name, []):
                    existing_articles[item["src_kb_article_id"]] = item

                # Handle unprocessed keys (throttling) with max retries
                unprocessed = response.get("UnprocessedKeys", {})
                retry_count = 0
                max_retries = 5
                while unprocessed and retry_count < max_retries:
                    retry_count += 1
                    log.warn(
                        "Retrying unprocessed DynamoDB batch read keys",
                        count=len(unprocessed.get(table.name, {}).get("Keys", [])),
                        retry=retry_count,
                        max_retries=max_retries,
                    )
                    response = dynamodb_resource.batch_get_item(
                        RequestItems=unprocessed
                    )
                    for item in response.get("Responses", {}).get(table.name, []):
                        existing_articles[item["src_kb_article_id"]] = item
                    unprocessed = response.get("UnprocessedKeys", {})

                if unprocessed:
                    log.error(
                        "Max retries exceeded for unprocessed DynamoDB keys",
                        remaining_count=len(
                            unprocessed.get(table.name, {}).get("Keys", [])
                        ),
                        max_retries=max_retries,
                    )

            except Exception as e:
                log.warn(
                    "Batch DynamoDB read failed, will fallback to individual reads",
                    batch_start=i,
                    error=str(e),
                )

        log.info(
            "Batch read complete",
            tenant_id=tid,
            requested=len(article_ids),
            found=len(existing_articles),
        )

    for article in articles:
        article_id = article.get("src_kb_article_id", "")
        if not article_id:
            continue

        # Check DynamoDB for cached language and protected status (from batch read)
        existing = existing_articles.get(article_id)
        cached_language = None

        # Skip articles in protected statuses (under review or already approved/rejected)
        if existing and existing.get("pipeline_status") in pstatuses:
            stats["skipped_protected"] = stats.get("skipped_protected", 0) + 1
            log.info(
                "Skipping protected article",
                article_id=article_id,
                status=existing["pipeline_status"],
            )
            continue

        # Get cached language to avoid redundant Comprehend API calls
        if existing:
            cached_language = existing.get("language")
            if cached_language:
                stats["cached_language"] = stats.get("cached_language", 0) + 1
            # If article exists, delete its vector so classify_embed re-embeds it
            try:
                delete_vector(article_id, ctx=ctx)
            except Exception as e:
                log.warn("Vector delete failed", article_id=article_id, error=str(e))

        # RELEASE 1: All articles are English - no language detection/filtering
        metadata = extract_metadata(
            article, source_system, cached_language=cached_language
        )
        detected_lang = metadata.get("language", "en")
        stats["language_breakdown"][detected_lang] = (
            stats["language_breakdown"].get(detected_lang, 0) + 1
        )

        # COMMENTED OUT FOR RELEASE 1: Language filtering disabled
        # All articles assumed to be English in Release 1
        # if detected_lang != 'en':
        #     stats['language_filtered'] += 1
        #     continue

        # Point to source S3 (data team's bucket) - NO upload needed
        source_bucket = article.get("_source_s3_bucket", SOURCE_BUCKET)
        source_key = article.get(
            "_source_s3_key", f"{tenant_s3_prefix(tid)}{article_id}.json"
        )
        source_file_path = f"s3://{source_bucket}/{source_key}"

        # Write DynamoDB record
        item = {
            "tenant_id": tenant_id_value,
            "src_kb_article_id": article_id,
            "tenant_code": tid,
            "source_system": source_system,
            "source_file_name": f"{article_id}.json",
            "source_file_path": source_file_path,
            "source_origin_path": source_file_path,
            "source_checksum": "",  # Not computed - change detection uses last_updated_ts_utc from Glue
            "source_deleted": False,
            "source_version": 1,
            "pipeline_status": "RAW",
            "processing_version": 1,
            "needs_reprocessing": False,
            "home_region": home_region or "",
            "media_map": None,  # Will be extracted by classify_embed
            "created_at": now,
            "updated_at": now,
            **metadata,
        }
        table.put_item(Item=item)

        to_process.append(
            {
                "article_id": article_id,
                "source_file_path": source_file_path,
                "reason": "NEW",
            }
        )
        stats["ingested"] += 1
        log.info(
            "Article ingested (light) - pointing to source S3",
            article_id=article_id,
            source_file_path=source_file_path,
        )

    return to_process, stats
