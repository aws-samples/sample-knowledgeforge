"""
Phase 1 — Classify & Embed

Takes every RAW article from DynamoDB, extracts the text from the HTML file
in S3, then runs two things: LLM-based classification (SOP, FAQ, Troubleshooting,
or Runbook) using a Bedrock managed prompt, and a Titan embedding for vector
similarity. Results are stored back in DynamoDB and the embedding is pushed to
S3 Vectors. Articles move from RAW → EMBEDDED after this phase.

Runs articles in parallel using a thread pool with throttle/retry logic to
handle Bedrock rate limits gracefully.
"""

import boto3
import json
import io
import time
import random

# Use a CSPRNG-backed generator (SystemRandom) for jitter/sampling so static
# analysis does not flag the default non-cryptographic PRNG (CWE-338).
_secure_random = random.SystemRandom()
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import unquote_plus
from botocore.exceptions import ClientError
from appconfig_loader import load_config
from logger import get_logger
from token_tracker import TokenTracker
from validation import validate_event, validate_article, ValidationError
from html_utils import extract_plain_text

# ── Load config from AppConfig (no fallback defaults) ─────────────────────────
CFG = load_config()
REGION = CFG.get("_metadata", {}).get("region", "")

s3 = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")
from botocore.config import Config as BotoConfig

bedrock = boto3.client(
    "bedrock-runtime",
    config=BotoConfig(retries={"mode": "adaptive", "total_max_attempts": 10}),
)
s3vectors = boto3.client("s3vectors")

TABLE_NAME = CFG.get("resources", {}).get("table_name", "")
BUCKET_NAME = CFG.get("resources", {}).get("pipeline_bucket", "")
VECTOR_BUCKET = CFG.get("resources", {}).get("vector_bucket", "")
VECTOR_INDEX = CFG.get("resources", {}).get("vector_index", "")
TENANT_ID = CFG.get("resources", {}).get("tenant_id", "")

TITAN_MODEL_ID = CFG.get("models", {}).get(
    "embedding_model_id", "amazon.titan-embed-text-v2:0"
)

# ── Prompt ARNs from AppConfig (Bedrock Managed Prompts) ──────────────────────
PROMPT_ARNS = CFG.get("prompts", {})

MAX_WORKERS = CFG.get("pipeline", {}).get("max_workers_phase1", 6)
THROTTLE_BASE_DELAY = CFG.get("pipeline", {}).get("throttle_base_delay", 5.0)
THROTTLE_MAX_RETRIES = CFG.get("pipeline", {}).get("throttle_max_retries", 5)

# ── Structured logger and token tracker (module-level for warm start reuse) ───
log = get_logger(tenant_id=TENANT_ID, lambda_name="classify_embed")
token_tracker = TokenTracker(logger=log)

# ── Guardrail config from AppConfig ───────────────────────────────────────────
GUARDRAIL_CFG = CFG.get("guardrail", {})
GUARDRAIL_CONFIG = None
if GUARDRAIL_CFG.get("guardrail_id") and GUARDRAIL_CFG.get("guardrail_version"):
    GUARDRAIL_CONFIG = {
        "guardrailIdentifier": GUARDRAIL_CFG["guardrail_id"],
        "guardrailVersion": str(GUARDRAIL_CFG["guardrail_version"]),
        "trace": "enabled",
    }


def _init_config(tenant_code, home_region=None, tenant_uuid=None):
    """Re-initialize module globals from tenant-specific config."""
    global CFG, TABLE_NAME, BUCKET_NAME, VECTOR_BUCKET, VECTOR_INDEX
    global TENANT_ID, TITAN_MODEL_ID, PROMPT_ARNS
    global MAX_WORKERS, THROTTLE_BASE_DELAY, THROTTLE_MAX_RETRIES
    global GUARDRAIL_CONFIG, log, token_tracker
    CFG = load_config(tenant_code=tenant_code, home_region=home_region)
    TABLE_NAME = CFG["resources"]["table_name"]
    BUCKET_NAME = CFG["resources"]["pipeline_bucket"]
    VECTOR_BUCKET = CFG["resources"]["vector_bucket"]
    VECTOR_INDEX = CFG["resources"]["vector_index"]
    # Use UUID from event if available, otherwise fall back to AppConfig value
    TENANT_ID = tenant_uuid or CFG["resources"]["tenant_id"]

    # Validate required configuration from AppConfig
    if not TABLE_NAME:
        raise ValueError("table_name is required in AppConfig resources")
    if not BUCKET_NAME:
        raise ValueError("pipeline_bucket is required in AppConfig resources")
    if not VECTOR_BUCKET:
        raise ValueError("vector_bucket is required in AppConfig resources")
    if not VECTOR_INDEX:
        raise ValueError("vector_index is required in AppConfig resources")
    if not TENANT_ID:
        raise ValueError("tenant_id is required in AppConfig resources")

    TITAN_MODEL_ID = CFG["models"]["embedding_model_id"]
    PROMPT_ARNS = CFG.get("prompts", {})
    MAX_WORKERS = CFG["pipeline"]["max_workers_phase1"]
    THROTTLE_BASE_DELAY = CFG["pipeline"]["throttle_base_delay"]
    THROTTLE_MAX_RETRIES = CFG["pipeline"]["throttle_max_retries"]
    log = get_logger(tenant_id=TENANT_ID, lambda_name="classify_embed")
    token_tracker = TokenTracker(logger=log)
    gr = CFG.get("guardrail", {})
    GUARDRAIL_CONFIG = None
    if gr.get("guardrail_id") and gr.get("guardrail_version"):
        GUARDRAIL_CONFIG = {
            "guardrailIdentifier": gr["guardrail_id"],
            "guardrailVersion": str(gr["guardrail_version"]),
            "trace": "enabled",
        }


class GuardrailBlockedError(Exception):
    """Raised when Bedrock guardrail intervenes and blocks the output."""

    def __init__(self, call_type, article_id, trace_info=None):
        self.call_type = call_type
        self.article_id = article_id
        self.trace_info = trace_info or {}
        super().__init__(f"Guardrail blocked {call_type} for article {article_id}")


def invoke_with_retry(fn, *args, **kwargs):
    """Call fn with retry for ModelErrorException only.
    ThrottlingException and ServiceUnavailableException are handled by
    boto3 adaptive retry mode configured on the bedrock client.
    """
    for attempt in range(THROTTLE_MAX_RETRIES):
        try:
            return fn(*args, **kwargs)
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code == "ModelErrorException":
                wait = THROTTLE_BASE_DELAY * (2**attempt) + _secure_random.uniform(0, 1)
                log.warn(
                    "ModelErrorException, retrying",
                    attempt=attempt + 1,
                    max_retries=THROTTLE_MAX_RETRIES,
                    wait_seconds=round(wait, 1),
                )
                time.sleep(wait)
            else:
                raise
    raise RuntimeError(
        f"Max retries ({THROTTLE_MAX_RETRIES}) exceeded on ModelErrorException"
    )


def parse_converse_response(response, call_type, article_id):
    """Extract text and record token usage from a Converse API response.
    Raises GuardrailBlockedError if the guardrail intervened."""
    usage = response.get("usage", {})
    token_tracker.record(
        call_type,
        article_id,
        usage.get("inputTokens", 0),
        usage.get("outputTokens", 0),
    )

    # Check if guardrail blocked the output
    stop_reason = response.get("stopReason", "")
    if stop_reason == "guardrail_intervened":
        trace_info = response.get("trace", {}).get("guardrail", {})
        log.warn(
            "Guardrail intervened",
            article_id=article_id,
            call_type=call_type,
            trace=json.dumps(trace_info, default=str)[:1000],
        )
        raise GuardrailBlockedError(call_type, article_id, trace_info)

    # Safely extract result text with proper error handling
    try:
        result_text = response["output"]["message"]["content"][0]["text"].strip()
    except (KeyError, IndexError, TypeError) as e:
        log.error(
            "Invalid Converse API response structure",
            article_id=article_id,
            call_type=call_type,
            error=str(e),
            response_keys=list(response.keys()),
        )
        raise ValueError(f"Invalid Converse API response structure: {e}")

    if result_text.startswith("```"):
        result_text = result_text.split("```")[1]
        if result_text.startswith("json"):
            result_text = result_text[4:]
        result_text = result_text.strip()

    json_match = re.search(r"\{.*\}", result_text, re.DOTALL)
    if json_match:
        result_text = json_match.group(0)

    return result_text


def get_raw_articles(table, limit=None):
    """Query DynamoDB for articles with status=RAW using pipeline-status-index GSI.
    Also picks up CLASSIFIED articles where classification succeeded but
    embedding was interrupted (e.g., Lambda timeout mid-flight).
    """
    articles = []
    for status in ("RAW", "CLASSIFIED"):
        kwargs = {
            "IndexName": "pipeline-status-index",
            "KeyConditionExpression": boto3.dynamodb.conditions.Key("tenant_id").eq(
                TENANT_ID
            )
            & boto3.dynamodb.conditions.Key("pipeline_status").eq(status),
        }
        if limit:
            kwargs["Limit"] = limit
        while True:
            response = table.query(**kwargs)
            articles.extend(response.get("Items", []))
            if limit or "LastEvaluatedKey" not in response:
                break
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    log.info("Fetched RAW/CLASSIFIED articles", count=len(articles))
    return articles


def extract_text_from_s3(bucket, key):
    """Read article from S3 - handles both JSON and HTML files.
    For JSON: extracts full_text field
    For HTML: reads HTML content directly
    """
    log.info("Reading file from S3", bucket=bucket, key=key)
    response = s3.get_object(Bucket=bucket, Key=key)
    content = response["Body"].read().decode("utf-8")
    log.info("File read successful", key=key, size_bytes=len(content))

    # Check if it's a JSON file
    if key.lower().endswith(".json"):
        try:
            data = json.loads(content)
            # Extract full_text from JSON (handles various JSON structures)
            from html_utils import get_article_content

            html_content, _ = get_article_content(data)
            if not html_content.strip():
                log.warn("No text content found in JSON", bucket=bucket, key=key)
                return ""
            text = extract_plain_text(html_content)
            log.info("Extracted text from JSON", key=key, text_length=len(text))
            return text
        except json.JSONDecodeError as e:
            log.error("JSON decode error", bucket=bucket, key=key, error=str(e))
            raise
    else:
        # Assume it's HTML
        text = extract_plain_text(content)
        if not text.strip():
            log.warn("Extracted text is empty", bucket=bucket, key=key)
        return text


def classify_article(text, article_id=None):
    """Invoke Claude Sonnet 4.5 via Converse API with managed prompt to classify the article."""
    max_chars = CFG["truncation"]["classification_max_chars"]
    truncated = text[:max_chars] if len(text) > max_chars else text

    def sanitise(t):
        return t.replace("{", "(").replace("}", ")").replace("\\", " ")

    log.debug(
        "Calling Bedrock for classification",
        article_id=article_id,
        text_length=len(truncated),
    )

    response = invoke_with_retry(
        bedrock.converse,
        modelId=PROMPT_ARNS["classification"],
        promptVariables={
            "article_content": {"text": sanitise(truncated)},
        },
    )

    result_text = parse_converse_response(response, "classify", article_id)
    log.debug(
        "Classification raw response",
        article_id=article_id,
        response_preview=result_text[:300],
    )

    result = json.loads(result_text)

    classification = result.get("classification")
    confidence = result.get("confidence")

    if not classification:
        raise ValueError(
            f"Bedrock did not return a classification. Raw response: {result_text}"
        )

    return classification, int(confidence)


def generate_embedding(text, article_id=None):
    """Invoke Titan V2 to generate embedding."""
    max_chars = CFG["truncation"]["embedding_max_chars"]
    input_text = text[:max_chars] if len(text) > max_chars else text

    response = invoke_with_retry(
        bedrock.invoke_model,
        modelId=TITAN_MODEL_ID,
        body=json.dumps(
            {
                "inputText": input_text,
                "dimensions": CFG["models"]["embedding_dimensions"],
                "normalize": CFG["models"]["embedding_normalize"],
            }
        ),
        contentType="application/json",
        accept="application/json",
    )
    body = json.loads(response["body"].read())
    embed_input_tokens = body.get("inputTextTokenCount", 0)
    token_tracker.record("embed", article_id, embed_input_tokens, 0)
    return body["embedding"]


def vector_exists(article_id):
    """Check if a vector already exists for this article_id."""
    try:
        response = s3vectors.get_vectors(
            vectorBucketName=VECTOR_BUCKET,
            indexName=VECTOR_INDEX,
            keys=[article_id],
            returnData=False,
        )
        return len(response.get("vectors", [])) > 0
    except Exception:
        return False


def store_vector(article_id, embedding, classification, source_system):
    """Store vector in S3 Vectors index. Skips if vector already exists (idempotent)."""
    if vector_exists(article_id):
        log.info("Vector already exists, skipping", article_id=article_id)
        return

    s3vectors.put_vectors(
        vectorBucketName=VECTOR_BUCKET,
        indexName=VECTOR_INDEX,
        vectors=[
            {
                "key": article_id,
                "data": {"float32": embedding},
                "metadata": {
                    "tenant_id": TENANT_ID,
                    "article_id": article_id,
                    "status": "PENDING",
                    "classification": classification,
                    "source_system": source_system,
                },
            }
        ],
    )


def process_article(article, now, job_id):
    """Process a single article: classify, embed, store vector, update DynamoDB."""
    table = dynamodb.Table(TABLE_NAME)
    article_id = article["src_kb_article_id"]
    source_file_path = article["source_file_path"]
    source_system = article.get("source_system", "ITSM_KB")
    current_status = article.get("pipeline_status", "RAW")

    # If vector already exists (from a previous incomplete run), read its metadata
    # and update DynamoDB without re-classifying
    if vector_exists(article_id):
        log.info("Vector exists, recovering from previous run", article_id=article_id)
        classification_from_vector = None
        try:
            vec_resp = s3vectors.get_vectors(
                vectorBucketName=VECTOR_BUCKET,
                indexName=VECTOR_INDEX,
                keys=[article_id],
                returnMetadata=True,
                returnData=False,
            )
            vec_meta = vec_resp.get("vectors", [{}])[0].get("metadata", {})
            classification_from_vector = vec_meta.get("classification", "")
            table.update_item(
                Key={"tenant_id": TENANT_ID, "src_kb_article_id": article_id},
                UpdateExpression="SET #st = :status, classification = :c, job_id = :job_id, updated_at = :now",
                ExpressionAttributeNames={"#st": "pipeline_status"},
                ExpressionAttributeValues={
                    ":status": "EMBEDDED",
                    ":c": classification_from_vector,
                    ":job_id": job_id,
                    ":now": now,
                },
            )
        except Exception:
            table.update_item(
                Key={"tenant_id": TENANT_ID, "src_kb_article_id": article_id},
                UpdateExpression="SET #st = :status, job_id = :job_id, updated_at = :now",
                ExpressionAttributeNames={"#st": "pipeline_status"},
                ExpressionAttributeValues={
                    ":status": "EMBEDDED",
                    ":job_id": job_id,
                    ":now": now,
                },
            )
        return {
            "article_id": article_id,
            "skipped": True,
            "classification": classification_from_vector,
            "confidence": None,
        }

    # Parse S3 URI to extract bucket and key (source files can be in different bucket)
    if source_file_path.startswith("s3://"):
        s3_uri_parts = source_file_path.replace("s3://", "").split("/", 1)
        source_bucket = s3_uri_parts[0]
        s3_key = unquote_plus(s3_uri_parts[1]) if len(s3_uri_parts) > 1 else ""
    else:
        # Fallback: assume it's in pipeline bucket
        source_bucket = BUCKET_NAME
        s3_key = unquote_plus(source_file_path)

    log.info(
        "Processing article",
        article_id=article_id,
        source_bucket=source_bucket,
        s3_key=s3_key,
        current_status=current_status,
    )

    # Read article from S3 - handle missing files explicitly
    try:
        text = extract_text_from_s3(source_bucket, s3_key)
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchKey":
            log.error(
                "S3 file not found - article exists in metadata but file is missing",
                article_id=article_id,
                bucket=source_bucket,
                key=s3_key,
                source_file_path=source_file_path,
            )

            # Clear error_message if this was a recovered article that's missing again
            update_expr = "SET #st = :status, error_message = :err, job_id = :job_id, updated_at = :now"
            if current_status == "ERROR_S3_FILE_MISSING":
                # File still missing after recovery attempt
                log.warn(
                    "File still missing after recovery attempt", article_id=article_id
                )

            table.update_item(
                Key={"tenant_id": TENANT_ID, "src_kb_article_id": article_id},
                UpdateExpression=update_expr,
                ExpressionAttributeNames={"#st": "pipeline_status"},
                ExpressionAttributeValues={
                    ":status": "ERROR_S3_FILE_MISSING",
                    ":err": f"S3 file not found: {source_file_path}",
                    ":job_id": job_id,
                    ":now": now,
                },
            )
            return {
                "article_id": article_id,
                "skipped": True,
                "s3_missing": True,
                "classification": None,
                "confidence": None,
            }
        else:
            # Re-raise other S3 errors (permissions, throttling, etc.)
            raise
    if not text.strip():
        log.warn(
            "No text content in article — setting to NO_CONTENT",
            article_id=article_id,
            s3_key=s3_key,
        )
        table.update_item(
            Key={"tenant_id": TENANT_ID, "src_kb_article_id": article_id},
            UpdateExpression="SET #st = :status, error_message = :err, job_id = :job_id, updated_at = :now",
            ExpressionAttributeNames={"#st": "pipeline_status"},
            ExpressionAttributeValues={
                ":status": "NO_CONTENT",
                ":err": f"No text extracted from {s3_key}",
                ":job_id": job_id,
                ":now": now,
            },
        )
        return {
            "article_id": article_id,
            "skipped": True,
            "no_content": True,
            "classification": None,
            "confidence": None,
        }

    try:
        classification, confidence = classify_article(text, article_id=article_id)
    except GuardrailBlockedError as gbe:
        log.warn(
            "Article blocked by guardrail during classification",
            article_id=article_id,
            call_type=gbe.call_type,
            trace=json.dumps(gbe.trace_info, default=str)[:500],
        )
        table.update_item(
            Key={"tenant_id": TENANT_ID, "src_kb_article_id": article_id},
            UpdateExpression="SET #st = :status, guardrail_reason = :gr, job_id = :job_id, updated_at = :now",
            ExpressionAttributeNames={"#st": "pipeline_status"},
            ExpressionAttributeValues={
                ":status": "GUARDRAIL_BLOCKED",
                ":gr": f"Blocked during classification: {json.dumps(gbe.trace_info, default=str)[:500]}",
                ":job_id": job_id,
                ":now": now,
            },
        )
        return {
            "article_id": article_id,
            "skipped": False,
            "classification": None,
            "confidence": None,
            "guardrail_blocked": True,
        }

    table.update_item(
        Key={"tenant_id": TENANT_ID, "src_kb_article_id": article_id},
        UpdateExpression="SET classification = :c, classification_confidence = :cc, classified_at = :t, #st = :status, job_id = :job_id, updated_at = :now",
        ExpressionAttributeNames={"#st": "pipeline_status"},
        ExpressionAttributeValues={
            ":c": classification,
            ":cc": confidence,
            ":t": now,
            ":status": "CLASSIFIED",
            ":job_id": job_id,
            ":now": now,
        },
    )

    embedding = generate_embedding(text, article_id=article_id)
    store_vector(article_id, embedding, classification, source_system)

    # Build token_usage map for DynamoDB (Phase 1 tokens: classify + embed)
    article_tokens = token_tracker.get_article(article_id)
    p1_in = article_tokens.get("classify_input", 0) + article_tokens.get(
        "embed_input", 0
    )
    p1_out = article_tokens.get("classify_output", 0)
    token_usage_map = {
        "classify_input": article_tokens.get("classify_input", 0),
        "classify_output": article_tokens.get("classify_output", 0),
        "embed_input": article_tokens.get("embed_input", 0),
        "phase1_total_input": p1_in,
        "phase1_total_output": p1_out,
        "total_input": p1_in,
        "total_output": p1_out,
    }

    # Clear error_message if this was a recovered article (previously ERROR_S3_FILE_MISSING)
    update_expr = "SET embedding_model = :em, embedding_dimensions = :ed, embedded_at = :t, #st = :status, job_id = :job_id, updated_at = :now, token_usage = :tu"
    expr_values = {
        ":em": TITAN_MODEL_ID,
        ":ed": CFG["models"]["embedding_dimensions"],
        ":t": now,
        ":status": "EMBEDDED",
        ":job_id": job_id,
        ":now": now,
        ":tu": token_usage_map,
    }

    if current_status == "ERROR_S3_FILE_MISSING":
        # Clear error_message for recovered articles
        update_expr += " REMOVE error_message"
        log.info(
            "Recovered article successfully processed, clearing error_message",
            article_id=article_id,
        )

    table.update_item(
        Key={"tenant_id": TENANT_ID, "src_kb_article_id": article_id},
        UpdateExpression=update_expr,
        ExpressionAttributeNames={"#st": "pipeline_status"},
        ExpressionAttributeValues=expr_values,
    )

    token_tracker.log_article_summary(article_id)
    log.info(
        "Article processed successfully",
        article_id=article_id,
        classification=classification,
        confidence=confidence,
    )
    return {
        "article_id": article_id,
        "skipped": False,
        "classification": classification,
        "confidence": confidence,
    }


def resolve_articles(event, table):
    """Resolve the list of articles to process.

    Distributed Map mode: event contains {"articles": [...]}
    Legacy mode: fall back to querying DynamoDB for RAW/CLASSIFIED articles.
    """
    batch = event.get("articles")
    if batch:
        articles = []
        for item in batch:
            aid = item.get("article_id")
            if not aid:
                continue
            resp = table.get_item(
                Key={"tenant_id": TENANT_ID, "src_kb_article_id": aid}
            )
            record = resp.get("Item")
            if record and record.get("pipeline_status") in ("RAW", "CLASSIFIED"):
                articles.append(record)
            elif record:
                log.info(
                    "Skipping article, not RAW/CLASSIFIED",
                    article_id=aid,
                    status=record.get("pipeline_status"),
                )
            else:
                log.warn("Article not found in DynamoDB", article_id=aid)
        log.info(
            "Distributed Map articles resolved",
            resolved=len(articles),
            batch_size=len(batch),
        )
        return articles

    # Legacy fallback
    log.info("Legacy mode: querying DynamoDB for RAW/CLASSIFIED")
    return get_raw_articles(table)


def lambda_handler(event, context):
    # Initialize tenant-specific config from event payload (uses tenant_code for AppConfig)
    _init_config(event.get("tenant_code", ""), home_region=event.get("home_region"), tenant_uuid=event.get("tenant_id", ""))

    # TENANT_ID is now set correctly inside _init_config (UUID from event takes priority)
    global TENANT_ID

    global token_tracker
    token_tracker = TokenTracker(logger=log)

    # Set job_id for correlation across all log lines
    job_id = event.get("job_id", datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S"))
    log.set_job_id(job_id)

    # Input validation
    try:
        validate_event(event)
    except ValidationError as ve:
        log.error("Event validation failed", error=str(ve))
        return {"error": str(ve)}

    table = dynamodb.Table(TABLE_NAME)
    now = datetime.now(timezone.utc).isoformat()

    articles = resolve_articles(event, table)

    stats = {
        "phase": "PHASE_1_CLASSIFY_EMBED",
        "tenant_id": TENANT_ID,
        "total_fetched": len(articles),
        "successfully_classified": 0,
        "successfully_embedded": 0,
        "successfully_stored_vector": 0,
        "failed": 0,
        "s3_files_missing": 0,
        "classification_breakdown": {
            "SOP": 0,
            "FAQ": 0,
            "Troubleshooting": 0,
            "RCA": 0,
            "Runbook": 0,
        },
        "avg_confidence": 0,
        "errors": [],
        "started_at": now,
    }
    confidence_scores = []

    try:
        workers = min(MAX_WORKERS, len(articles))
        workers = max(workers, 1)
        log.info(
            "Starting classify/embed pass", workers=workers, article_count=len(articles)
        )

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {}
            for article in articles:
                futures[executor.submit(process_article, article, now, job_id)] = (
                    article
                )

            for future in as_completed(futures):
                article_id = futures[future]["src_kb_article_id"]
                try:
                    result = future.result(
                        timeout=CFG.get("pipeline", {}).get(
                            "phase1_article_timeout", 120
                        )
                    )

                    # Check if S3 file was missing
                    if result.get("s3_missing"):
                        stats["s3_files_missing"] += 1
                        log.warn(
                            "Article marked as ERROR_S3_FILE_MISSING",
                            article_id=article_id,
                        )
                        continue

                    stats["successfully_classified"] += 1
                    stats["successfully_embedded"] += 1
                    stats["successfully_stored_vector"] += 1
                    if not result["skipped"] and result["classification"]:
                        stats["classification_breakdown"][result["classification"]] = (
                            stats["classification_breakdown"].get(
                                result["classification"], 0
                            )
                            + 1
                        )
                        confidence_scores.append(result["confidence"])
                except TimeoutError:
                    log.error("Article processing timeout", article_id=article_id)
                    stats["failed"] += 1
                    stats["errors"].append(
                        {"article_id": article_id, "error": "future.result() timeout"}
                    )
                except Exception as e:
                    log.error(
                        "Article processing error", article_id=article_id, error=str(e)
                    )
                    stats["failed"] += 1
                    stats["errors"].append({"article_id": article_id, "error": str(e)})

    except Exception as handler_err:
        log.error("Handler error", error=str(handler_err))
        stats["errors"].append({"error": f"HANDLER: {str(handler_err)}"})

    stats["avg_confidence"] = (
        round(sum(confidence_scores) / len(confidence_scores), 1)
        if confidence_scores
        else 0
    )
    stats["completed_at"] = datetime.now(timezone.utc).isoformat()

    token_summary = token_tracker.log_grand_summary()
    stats["token_usage"] = token_summary

    log.info("Phase 1 complete", stats=stats)

    return {"tenant_id": TENANT_ID, "phase1_stats": stats}
