"""
Lambda: SN Task Creator

Triggered by DynamoDB Streams on the article_metadata table.
When an article's status changes to GENERATED, this Lambda:
  1. Loads tenant config from AppConfig (ServiceNow URL, API key ARN, etc.)
  2. Checks review_limit against reviews_sent counter in pipeline_job_status
  3. Reads the lean JSON payload from S3: {tenant}/generated/{article_id}.json
  4. Reads full article record from DynamoDB for metadata
  5. Applies outbound field mapping (DW names → SN names) from tenant AppConfig
  6. Sends to ServiceNow catalog API:
     - ITSM_KB: article_id + enriched_text + kb_score
     - Problem Finder: full metadata + enriched_text + kb_score
  7. Updates article_metadata status to REQUEST_CREATED
  8. Atomically increments reviews_sent in pipeline_job_status

ServiceNow config comes from tenant AppConfig (servicenow block):
  catalog_api_url, api_key_secret_arn, kms_account_id, review_limit
  caller_email, catalog_item_id → fetched dynamically from tenant-connector API

review_limit behaviour:
  -1 = disabled, no articles sent for review
  0 = send all articles (no limit tracking)
  N = send at most N articles across ALL invocations for a given tenant+job_id.

Environment variables (infrastructure only):
  S3_BUCKET               - Pipeline bucket name
  ARTICLE_TABLE           - DynamoDB article_metadata table name
  JOB_STATUS_TABLE        - DynamoDB pipeline_job_status table name
"""

import json
import os
import boto3
import base64
import urllib3
import logging
from decimal import Decimal
from datetime import datetime, timezone
from appconfig_loader import load_config
from tenant_connector import get_connector_config

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Infrastructure env vars (shared across all tenants)
S3_BUCKET = os.environ.get("S3_BUCKET")
ARTICLE_TABLE = os.environ.get("ARTICLE_TABLE")
JOB_STATUS_TABLE = os.environ.get("JOB_STATUS_TABLE")
REGION = os.environ.get("AWS_REGION", "")
TENANT_CONNECTOR_API_URL = os.environ.get("TENANT_CONNECTOR_API_URL", "")
TENANT_CONNECTOR_API_HOST = os.environ.get("TENANT_CONNECTOR_API_HOST", "")

# Validate required environment variables
if not S3_BUCKET:
    raise ValueError("S3_BUCKET environment variable is required")
if not ARTICLE_TABLE:
    raise ValueError("ARTICLE_TABLE environment variable is required")
if not JOB_STATUS_TABLE:
    raise ValueError("JOB_STATUS_TABLE environment variable is required")

s3 = boto3.client("s3", region_name=REGION)
dynamodb = boto3.resource("dynamodb", region_name=REGION)
secrets = boto3.client("secretsmanager", region_name=REGION)
kms_client = boto3.client("kms", region_name=REGION)
article_table = dynamodb.Table(ARTICLE_TABLE)
job_status_table = dynamodb.Table(JOB_STATUS_TABLE)
http = urllib3.PoolManager()

ENV_CODE = os.environ.get("ENV_CODE", "d")
REGION_CODE = os.environ.get("REGION_CODE", "euw1")

_api_key_cache = {}  # {secret_arn: api_key}
_config_cache = {}


def _mint_trust_key(tenant_code, caller_email, job_id, home_region=None, tenant_uuid=None):
    """Mint a KMS-encrypted trust key token for the automation API.
    Same pattern as WS1 Connect Lambda — encrypts a JSON payload with the
    tenant-specific KMS key and returns base64-encoded ciphertext.
    """
    sn_cfg = _get_sn_config(tenant_code, home_region)
    kms_account_id = sn_cfg.get("kms_account_id", "")

    # Build alias: {tenant_code}-{env_code}-{region_code}-{home_region}-tenant-key
    alias_name = f"{tenant_code}-{ENV_CODE}-{REGION_CODE}-{home_region or REGION}-tenant-key"
    key_arn = f"arn:aws:kms:{REGION}:{kms_account_id}:alias/{alias_name}"

    payload = {
        "requester_email": caller_email,
        "caller_email": caller_email,
        "tenant_code": tenant_code,
        "channel": "article-curation",
        "platform_role": "basic_user",
        "timestamp_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ttl_seconds": 86400,
        "contact_id": f"article-curation-{job_id}",
        "session_id": job_id,
    }

    encryption_context = {"tenant_code": tenant_code}
    plaintext = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    logger.info(
        f"TRUST_KEY_MINT: payload={json.dumps(payload)}, "
        f"encryption_context={json.dumps(encryption_context)}, "
        f"key_arn={key_arn}"
    )
    response = kms_client.encrypt(
        KeyId=key_arn,
        Plaintext=plaintext,
        EncryptionContext=encryption_context,
    )
    return base64.b64encode(response["CiphertextBlob"]).decode("utf-8")


def _get_config(tenant_id, home_region=None):
    """Get configuration for the given tenant, with caching."""
    cache_key = f"{tenant_id}_{home_region or ''}"
    if cache_key not in _config_cache:
        try:
            logger.info(
                f"Loading config for tenant: {tenant_id}, home_region: {home_region}"
            )
            _config_cache[cache_key] = load_config(
                tenant_code=tenant_id, home_region=home_region
            )
            logger.info(f"Config loaded successfully for {tenant_id}")
        except Exception as e:
            logger.error(f"Config loading error for {tenant_id}: {e}")
            raise
    return _config_cache[cache_key]


def _get_sn_config(tenant_id, home_region=None):
    """Get ServiceNow config from tenant AppConfig."""
    config = _get_config(tenant_id, home_region)
    return config.get("servicenow", {})


def _map_to_sn(dw_field, tenant_id, home_region=None):
    """Map a data warehouse field name to ServiceNow field name."""
    config = _get_config(tenant_id, home_region)
    outbound_mapping = config.get("field_mapping", {}).get("outbound", {})
    return outbound_mapping.get(dw_field, dw_field)


def _get_api_key(tenant_id, home_region=None):
    """Fetch x-api-key from Secrets Manager. Cached per secret ARN for warm Lambda reuse."""
    sn_config = _get_sn_config(tenant_id, home_region)
    secret_arn = sn_config.get("api_key_secret_arn", "")
    if not secret_arn:
        raise ValueError(f"No api_key_secret_arn configured for tenant {tenant_id}")

    if secret_arn not in _api_key_cache:
        try:
            resp = secrets.get_secret_value(SecretId=secret_arn)
            secret_string = resp["SecretString"]
            secret = json.loads(secret_string)
            _api_key_cache[secret_arn] = secret.get(
                "x-api-key", secret.get("api_key", "")
            )
        except json.JSONDecodeError as e:
            logger.error(f"JSON decode error in Secrets Manager: {e}")
            raise
    return _api_key_cache[secret_arn]


def _get_review_limit(tenant_id, home_region=None):
    """Get review_limit from tenant AppConfig."""
    sn_config = _get_sn_config(tenant_id, home_region)
    limit = sn_config.get("review_limit", 0)
    return int(limit)


def _try_reserve_review_slot(tenant_id, job_id, home_region=None, tenant_code=None):
    """Atomically reserve a review slot using increment-first approach.
    This ensures no race condition by using DynamoDB's atomic ADD operation.
    The counter is per-job per-tenant (resets for each new job run).
    Counter records auto-expire after 30 days via TTL.
    """
    # Use tenant_code for AppConfig resolution, tenant_id (UUID) for DynamoDB key
    config_key = tenant_code or tenant_id
    review_limit = _get_review_limit(config_key, home_region)
    if review_limit == -1:
        logger.info(f"REVIEW_LIMIT: Review disabled (limit=-1) for {tenant_id}")
        return False  # Review disabled, skip all
    if review_limit == 0:
        logger.info(f"REVIEW_LIMIT: Unlimited reviews (limit=0) for {tenant_id}")
        return True  # No limit, send all

    try:
        # Use composite key: tenant + job for per-job-run limiting
        # This allows 1 review per tenant per job, but resets for new jobs
        counter_key = {"tenant_id": tenant_id, "job_id": f"{job_id}_REVIEW_COUNTER"}

        # Calculate TTL: expire after 30 days (only for counter records)
        import time

        ttl_timestamp = int(time.time()) + (30 * 24 * 60 * 60)

        # Atomically increment the counter and get the new value
        resp = job_status_table.update_item(
            Key=counter_key,
            UpdateExpression="ADD reviews_sent :inc SET #ttl = :ttl, tenant_code = :tc, home_region = :hr",
            ExpressionAttributeNames={"#ttl": "ttl"},
            ExpressionAttributeValues={":inc": 1, ":ttl": ttl_timestamp, ":tc": tenant_code or "", ":hr": home_region or ""},
            ReturnValues="UPDATED_NEW",
        )

        new_count = int(resp["Attributes"]["reviews_sent"])

        if new_count <= review_limit:
            logger.info(
                f"REVIEW_LIMIT: Slot RESERVED ({new_count}/{review_limit}) for {tenant_id} [job: {job_id}] [TTL: 30 days]"
            )
            return True
        else:
            # Over limit - decrement back with retry
            for attempt in range(3):
                try:
                    job_status_table.update_item(
                        Key=counter_key,
                        UpdateExpression="ADD reviews_sent :dec",
                        ExpressionAttributeValues={":dec": -1},
                    )
                    logger.info(
                        f"REVIEW_LIMIT: Slot REJECTED ({new_count}/{review_limit}) for {tenant_id} [job: {job_id}] - decremented back"
                    )
                    break
                except Exception as dec_err:
                    if attempt < 2:
                        logger.warning(
                            f"REVIEW_LIMIT: Decrement retry {attempt + 1}/3 for {tenant_id}/{job_id}: {dec_err}"
                        )
                        import time as time_module

                        time_module.sleep(0.5 * (attempt + 1))
                    else:
                        logger.error(
                            f"REVIEW_LIMIT: Failed to decrement after 3 attempts for {tenant_id}/{job_id}: {dec_err}"
                        )
            return False

    except Exception as e:
        logger.error(
            f"REVIEW_LIMIT: ERROR reserving slot for {tenant_id}/{job_id}: {e}"
        )
        return False


def read_output_json(tenant_id, kb_number):
    key = f"{tenant_id}/generated/{kb_number}.json"
    logger.info("Reading output JSON from S3: bucket=%s, key=%s", S3_BUCKET, key)
    try:
        resp = s3.get_object(Bucket=S3_BUCKET, Key=key)
        content = resp["Body"].read().decode("utf-8")
        logger.info("S3 read successful: key=%s, size=%d bytes", key, len(content))
        return json.loads(content)
    except json.JSONDecodeError as e:
        logger.error(
            "JSON decode error in S3 file: bucket=%s, key=%s, error=%s, content_preview=%s",
            S3_BUCKET,
            key,
            e,
            repr(content[:500]),
        )
        raise
    except Exception as e:
        logger.error("S3 read failed: bucket=%s, key=%s, error=%s", S3_BUCKET, key, e)
        raise


def build_kb_score(payload):
    """Build KB SCORE dict from pipeline output payload.
    Retirement fields are only included if article is flagged for retirement.
    """
    kb_score = {
        "classification": payload.get("classification", ""),
        "classification_confidence": payload.get("classification_confidence", 0),
        "is_duplicate": payload.get("is_duplicate", False),
        "quality_score_before": payload.get("quality_score_before", 0),
        "quality_score_after": payload.get("quality_score_after", 0),
        "quality_passed": payload.get("quality_passed", False),
        "quality_issues": payload.get("quality_issues", []),
        "enrichment_summary": payload.get("enrichment_summary", ""),
    }

    # Only include retirement data if article is flagged for retirement
    retirement_flag = payload.get("retirement_flag", "NOT_FLAGGED")
    if retirement_flag != "NOT_FLAGGED":
        kb_score["retirement_flag"] = retirement_flag
        kb_score["retirement_freshness_days"] = payload.get(
            "retirement_freshness_days", 0
        )
        kb_score["retirement_llm_decision"] = payload.get(
            "retirement_llm_decision", False
        )
        kb_score["retirement_llm_reason"] = payload.get("retirement_llm_reason", "")
        kb_score["retirement_llm_confidence"] = payload.get(
            "retirement_llm_confidence", 0
        )

    return kb_score


def build_kb_json_existing(payload, tenant_id=""):
    """Build KB JSON for existing ITSM_KB articles.
    Maps src_kb_article_id to ServiceNow field name via outbound mapping.
    """
    article_id = payload.get("src_kb_article_id", "")
    return {
        "article_id": article_id,
        "source_system": payload.get("source_system", "ITSM_KB"),
        "enriched_text": payload.get("enriched_text", ""),
    }


def build_kb_json_new(payload, article_record, tenant_id):
    """Build KB JSON for new Problem Finder articles with full metadata."""
    rec = article_record or {}
    return {
        _map_to_sn("article_title", tenant_id): rec.get(
            "article_title", payload.get("short_description", "")
        ),
        _map_to_sn("full_text", tenant_id): payload.get("enriched_text", ""),
        _map_to_sn("kb_author", tenant_id): rec.get("kb_author", ""),
        _map_to_sn("status", tenant_id): rec.get("status", "Draft"),
        _map_to_sn("sys_class_name", tenant_id): rec.get(
            "sys_class_name", "kb_knowledge"
        ),
        _map_to_sn("sys_domain", tenant_id): rec.get("sys_domain", ""),
        _map_to_sn("language", tenant_id): rec.get("language", "en"),
        _map_to_sn("kb_valid_to_ts", tenant_id): rec.get(
            "kb_valid_to_ts", "2100-01-01"
        ),
        _map_to_sn("active", tenant_id): str(rec.get("active", "true")).lower(),
        _map_to_sn("description", tenant_id): rec.get(
            "description", rec.get("article_title", "")
        ),
        _map_to_sn("can_read_user_criteria", tenant_id): rec.get(
            "can_read_user_criteria", ""
        ),
        _map_to_sn("last_updated_ts_utc", tenant_id): rec.get(
            "last_updated_ts_utc", ""
        ),
        "source_system": payload.get("source_system", ""),
        "enriched_text": payload.get("enriched_text", ""),
    }


def send_to_servicenow(payload, article_record=None, tenant_id="", home_region=None, tenant_uuid=None):
    """Send article to ServiceNow catalog API.

    ServiceNow config comes from:
    - catalog_api_url, api_key_secret_arn, kms_account_id, review_limit → tenant AppConfig
    - caller_email, catalog_item_id → tenant-connector API (dynamic, cached)
    - tenant_uuid (for X-Tenant-ID) → passed from DynamoDB stream event
    """
    sn_config = _get_sn_config(tenant_id, home_region)
    catalog_api_url = sn_config.get("catalog_api_url", "")

    if not catalog_api_url:
        raise ValueError(f"No catalog_api_url configured for tenant {tenant_id}")

    if not tenant_uuid:
        raise ValueError(f"No tenant_uuid available for tenant {tenant_id} — check article_metadata record")

    # Fetch caller_email and catalog_item_id from tenant-connector API
    connector_config = None
    if TENANT_CONNECTOR_API_URL and TENANT_CONNECTOR_API_HOST:
        connector_config = get_connector_config(
            tenant_id=tenant_uuid,
            api_url=TENANT_CONNECTOR_API_URL,
            api_host=TENANT_CONNECTOR_API_HOST,
            region=REGION,
        )

    # Use connector API values, fall back to AppConfig
    caller_email = (
        connector_config.get("caller_email", "") if connector_config
        else sn_config.get("caller_email", "")
    )
    catalog_item_id = (
        connector_config.get("catalog_item_id", "") if connector_config
        else sn_config.get("catalog_item_id", "")
    )

    if not caller_email:
        raise ValueError(f"No caller_email available for tenant {tenant_id}")
    if not catalog_item_id:
        raise ValueError(f"No catalog_item_id available for tenant {tenant_id}")

    source_system = (article_record or {}).get("source_system", "ITSM_KB")

    if source_system == "ITSM_KB":
        kb_json = build_kb_json_existing(payload, tenant_id)
    else:
        kb_json = build_kb_json_new(payload, article_record, tenant_id)

    kb_score = build_kb_score(payload)

    body = {
        "data": {
            "catalog_item_id": catalog_item_id,
            "quantity": 1,
            "variables": {
                "kb_json": json.dumps(kb_json, ensure_ascii=False),
                "kb_score": json.dumps(kb_score, ensure_ascii=False),
            },
        }
    }

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "x-api-key": _get_api_key(tenant_id, home_region),
        "X-Tenant-ID": tenant_uuid,
        "X-Correlation-ID": payload.get("job_id", ""),
        "X-Platform-Caller-Email": caller_email,
        "X-Platform-Requester-Email": caller_email,
        "X-Platform-Contact-Id": f"article-curation-{payload.get('job_id', '')}",
        "X-Platform-Session-Id": payload.get("job_id", "article-curation-session"),
        "X-Platform-Trust-Key": _mint_trust_key(
            tenant_id,
            caller_email,
            payload.get("job_id", ""),
            home_region,
            tenant_uuid=tenant_uuid,
        ),
    }

    body_json = json.dumps(body).encode("utf-8")

    # Log full request payload before sending
    safe_headers = {k: v for k, v in headers.items() if k != "api-key"}
    safe_headers["api-key"] = "***"
    logger.info(
        "REVIEW_REQUEST: Sending article to ServiceNow — url=%s, headers=%s, payload=%s",
        catalog_api_url,
        json.dumps(safe_headers),
        body_json.decode()[:3000],
    )

    resp = http.request(
        "POST",
        catalog_api_url,
        body=body_json,
        headers=headers,
        timeout=30.0,
    )

    if resp.status not in (200, 201):
        safe_headers = {k: v for k, v in headers.items() if k != "x-api-key"}
        safe_headers["x-api-key"] = "***"
        logger.error(
            "ServiceNow API error — request details: "
            "url=%s, status=%d, response=%s, headers=%s, payload=%s",
            catalog_api_url,
            resp.status,
            resp.data.decode()[:1000],
            json.dumps(safe_headers),
            body_json.decode()[:2000],
        )
        raise Exception(
            f"ServiceNow catalog API failed: HTTP {resp.status} - {resp.data.decode()[:500]}"
        )

    response_body = json.loads(resp.data.decode())
    logger.info(
        "REVIEW_REQUEST: ServiceNow response received — status=%d, response=%s",
        resp.status,
        json.dumps(response_body)[:2000],
    )
    return response_body


def update_article_status(tenant_id, article_id, api_response):
    """Update article_metadata with RITM details from ServiceNow API response."""
    now = datetime.now(timezone.utc).isoformat()
    data = api_response.get("data", {})
    ritm_number = data.get("number", "")
    logger.info(
        "Updating article status: tenant=%s, article_id=%s, new_status=REQUEST_CREATED, ritm_number=%s",
        tenant_id,
        article_id,
        ritm_number,
    )
    article_table.update_item(
        Key={"tenant_id": tenant_id, "src_kb_article_id": article_id},
        UpdateExpression=(
            "SET #st = :status,"
            " ritm_id = :ritm_id,"
            " ritm_number = :ritm_number,"
            " ritm_request_id = :req_id,"
            " ritm_requested_for = :req_for,"
            " review_submitted_at = :now,"
            " updated_at = :now"
        ),
        ExpressionAttributeNames={"#st": "pipeline_status"},
        ExpressionAttributeValues={
            ":status": "REQUEST_CREATED",
            ":ritm_id": data.get("id", ""),
            ":ritm_number": data.get("number", ""),
            ":req_id": data.get("request_id", ""),
            ":req_for": data.get("requested_for", ""),
            ":now": now,
        },
    )


def process_article(tenant_code, article_id, kb_number, job_id, home_region=None, tenant_uuid=None, tenant_id=None):
    """Process a single article for ServiceNow submission.
    
    Args:
        tenant_code: Short code (e.g. 'bcme') — used for AppConfig, S3 paths
        tenant_id: UUID — used for DynamoDB PK
        tenant_uuid: Same as tenant_id — used for X-Tenant-ID header
    """
    # tenant_id (UUID) for DynamoDB operations; tenant_code for config/S3
    db_tenant_id = tenant_id or tenant_uuid or tenant_code
    logger.info(
        f"Processing: {kb_number} (tenant_code: {tenant_code}, tenant_id: {db_tenant_id}, home_region: {home_region})"
    )

    try:
        payload = read_output_json(db_tenant_id, kb_number)
        payload["job_id"] = job_id

        # Fetch full article record from DynamoDB for metadata
        logger.info(
            "Fetching article record from DynamoDB: tenant_id=%s, article_id=%s",
            db_tenant_id,
            article_id,
        )
        article_record = article_table.get_item(
            Key={"tenant_id": db_tenant_id, "src_kb_article_id": article_id}
        ).get("Item", {})
        logger.info(
            "Article record fetched: source_system=%s, has_record=%s",
            article_record.get("source_system", "unknown"),
            bool(article_record),
        )

        # Log retirement flag for visibility
        retirement_flag = payload.get("retirement_flag", "NOT_FLAGGED")
        if retirement_flag != "NOT_FLAGGED":
            logger.info(
                f"Article {kb_number} flagged for retirement: {retirement_flag} - retirement data included in kb_score"
            )
        else:
            logger.info(
                f"Article {kb_number} not flagged for retirement - sending for standard review"
            )

        api_response = send_to_servicenow(
            payload, article_record=article_record, tenant_id=tenant_code, home_region=home_region, tenant_uuid=tenant_uuid
        )
        ritm_number = api_response.get("data", {}).get("number", "N/A")
        logger.info(f"ServiceNow RITM created: {ritm_number}")

        update_article_status(db_tenant_id, article_id, api_response)
        logger.info(f"Article pipeline_status updated to REQUEST_CREATED")

        return {"number": kb_number, "ritm_number": ritm_number}

    except Exception as e:
        logger.error(f"ERROR processing {kb_number}: {e}")
        # If ServiceNow creation failed, release the reserved review slot
        if _get_review_limit(tenant_code, home_region) > 0:
            try:
                counter_key = {
                    "tenant_id": tenant_id,
                    "job_id": f"{job_id}_REVIEW_COUNTER",
                }
                job_status_table.update_item(
                    Key=counter_key,
                    UpdateExpression="ADD reviews_sent :dec",
                    ExpressionAttributeValues={":dec": -1},
                )
                logger.info(
                    f"REVIEW_LIMIT: Released slot due to ServiceNow error for {tenant_id}"
                )
            except Exception as dec_error:
                logger.error(f"REVIEW_LIMIT: Failed to release slot: {dec_error}")
        raise


def lambda_handler(event, context):
    processed = 0
    skipped = 0
    errors = 0

    for record in event.get("Records", []):
        if record.get("eventName") not in ("INSERT", "MODIFY"):
            continue

        new_image = record.get("dynamodb", {}).get("NewImage", {})
        old_image = record.get("dynamodb", {}).get("OldImage", {})

        new_status = new_image.get("pipeline_status", {}).get("S", "")
        old_status = old_image.get("pipeline_status", {}).get("S", "")

        if new_status != "GENERATED" or old_status == "GENERATED":
            continue

        tenant_id = new_image.get("tenant_id", {}).get("S", "")
        article_id = new_image.get("src_kb_article_id", {}).get("S", "")
        kb_number = new_image.get("src_kb_article_id", {}).get("S", "")
        job_id = new_image.get("job_id", {}).get("S", "")
        home_region = new_image.get("home_region", {}).get("S", "")
        tenant_code = new_image.get("tenant_code", {}).get("S", "")
        # tenant_id IS the UUID (it's the PK) — use it directly for X-Tenant-ID header
        tenant_uuid = tenant_id

        if not kb_number or not tenant_id:
            logger.warning(
                f"SKIP: missing tenant_code or src_kb_article_id for {article_id}"
            )
            errors += 1
            continue

        # Try to reserve a review slot atomically
        if not _try_reserve_review_slot(tenant_id, job_id, home_region, tenant_code=tenant_code):
            skipped += 1
            continue

        try:
            result = process_article(
                tenant_code, article_id, kb_number, job_id, home_region=home_region, tenant_uuid=tenant_uuid, tenant_id=tenant_id
            )
            if result is None:
                skipped += 1
            else:
                processed += 1
        except Exception as e:
            logger.error(f"ERROR processing {kb_number}: {e}")
            errors += 1

    return {"processed": processed, "skipped": skipped, "errors": errors}
