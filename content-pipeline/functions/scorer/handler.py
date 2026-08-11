"""
Phase 2 — Dedup, Quality, Enrich & Post-Score

The heaviest phase in the pipeline. For each EMBEDDED article it:
1. Checks for duplicates via cosine similarity against S3 Vectors — dupes get
   flagged and skipped.
2. Runs an LLM quality check that scores the article on weighted dimensions
   (grammar, readability, coherence, completeness, structure) and produces
   detailed feedback and issues.
3. Enriches the article paragraph-by-paragraph — rewriting weak paragraphs and
   optionally appending new structural sections — regardless of pass/fail.
4. Post-scores the enriched version using the same quality prompt to measure
   improvement.
5. Promotes articles from QUALITY_FAILED to GENERATED if the post-enrichment
   score crosses the quality threshold.

Enriched HTML files are written to S3 under generated/. All scores,
feedback, and token usage are persisted to DynamoDB. At the end of the run,
aggregate pipeline stats are computed and stored in the job status table.
"""

import boto3
import json
import io
import re
import time
import random

# Use a CSPRNG-backed generator (SystemRandom) for jitter/sampling so static
# analysis does not flag the default non-cryptographic PRNG (CWE-338).
_secure_random = random.SystemRandom()
import logging
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import unquote_plus
from botocore.exceptions import ClientError
from config_provider import load_config
from markup_tools import (
    extract_plain_text,
    parse_html_with_media_map,
    reinsert_media_tags,
    paragraphs_to_html,
    paragraphs_as_text_list,
    get_paragraph_text,
)
from logger import get_logger
from usage_tracker import TokenTracker
from validation import (
    validate_event,
    validate_article,
    validate_quality_response,
    validate_enrichment_response,
    ValidationError,
)

# ── Load config from AppConfig (no fallback defaults) ─────────────────────────
CFG = load_config()
REGION = CFG.get("_metadata", {}).get("region", "")

s3 = boto3.client("s3")
s3vectors = boto3.client("s3vectors")
dynamodb = boto3.resource("dynamodb")
from botocore.config import Config as BotoConfig

bedrock = boto3.client(
    "bedrock-runtime",
    config=BotoConfig(
        retries={"mode": "adaptive", "total_max_attempts": 10},
        read_timeout=300,  # 5 min — generous socket timeout; hard timeout enforced by _converse_with_timeout
    ),
)

# Hard wall-clock timeout for Bedrock Converse calls (seconds)
# read_timeout alone doesn't work reliably because Bedrock keeps TCP connections alive
# This uses a separate thread to enforce a strict deadline
BEDROCK_CALL_TIMEOUT = 300  # 5 min — if call doesn't complete, article goes to RAW


def _converse_with_timeout(kwargs, timeout_seconds):
    """Call bedrock.converse() with a hard wall-clock timeout.
    Uses a single-thread executor to enforce deadline since signal.alarm
    doesn't work inside ThreadPoolExecutor threads.
    Raises TimeoutError if the call exceeds timeout_seconds.
    """
    from concurrent.futures import ThreadPoolExecutor as _TPE, TimeoutError as _TE

    with _TPE(max_workers=1) as _executor:
        future = _executor.submit(bedrock.converse, **kwargs)
        try:
            return future.result(timeout=timeout_seconds)
        except _TE:
            raise TimeoutError(
                f"Read timeout on endpoint URL: bedrock converse exceeded {timeout_seconds}s wall-clock limit"
            )


TABLE_NAME = CFG.get("resources", {}).get("table_name", "")
INGEST_BUCKET = CFG.get("resources", {}).get("pipeline_bucket", "")
VECTOR_BUCKET = CFG.get("resources", {}).get("vector_bucket", "")
VECTOR_INDEX = CFG.get("resources", {}).get("vector_index", "")
TENANT_ID = CFG.get("resources", {}).get("tenant_id", "")

# ── Prompt ARNs from AppConfig (Bedrock Managed Prompts) ──────────────────────
PROMPT_ARNS = CFG.get("prompts", {})

# ── Prompt caching: resolve prompt content for direct Converse calls ──────────
LLM_MODEL_ID = CFG.get("models", {}).get("llm_model_id", "")
_bedrock_agent = boto3.client(
    "bedrock-agent",
    config=BotoConfig(retries={"mode": "adaptive", "total_max_attempts": 10}),
)
PROMPT_CONTENT = {}  # {key: {'system': str, 'user': str}}
_prompt_cache = {}  # {tenant_id: {key: {'system': str, 'user': str}}}
_init_logger = logging.getLogger("dedup.init")


def _resolve_prompt(key, arn):
    """Fetch system and user text from a managed prompt ARN.
    Retries with exponential backoff on throttling to handle concurrent Lambda cold starts.
    """
    parts = arn.split("/")
    prompt_id = parts[-1].split(":")[0]
    version = parts[-1].split(":")[1] if ":" in parts[-1] else None
    kwargs = {"promptIdentifier": prompt_id}
    if version:
        kwargs["promptVersion"] = version

    max_retries = 5
    for attempt in range(max_retries):
        try:
            detail = _bedrock_agent.get_prompt(**kwargs)
            variant = detail["variants"][0]
            chat = variant.get("templateConfiguration", {}).get("chat", {})
            sys_text = "".join(
                b.get("text", "") for b in chat.get("system", []) if "text" in b
            )
            user_text = ""
            for msg in chat.get("messages", []):
                if msg.get("role") == "user":
                    user_text = "".join(
                        c.get("text", "") for c in msg.get("content", [])
                    )
            return {"system": sys_text, "user": user_text}
        except Exception as e:
            if "ThrottlingException" in str(e) and attempt < max_retries - 1:
                import time as _time

                wait = (2**attempt) + _secure_random.uniform(0, 1)
                _init_logger.warning(
                    "GetPrompt throttled for %s, retry %d/%d in %.1fs",
                    key,
                    attempt + 1,
                    max_retries,
                    wait,
                )
                _time.sleep(wait)
                continue
            _init_logger.warning(
                "Failed to resolve prompt %s, falling back to managed prompt: %s",
                key,
                e,
            )
            return None


for _key, _arn in PROMPT_ARNS.items():
    resolved = _resolve_prompt(_key, _arn)
    if resolved:
        PROMPT_CONTENT[_key] = resolved


def _prompt_s3_key(tenant_id):
    """S3 key for cached prompt content."""
    return f"{tenant_id}/pipeline-cache/prompts.json"


def _load_prompts_from_s3(tenant_id):
    """Try to load cached prompt content from S3. Returns dict or empty dict on miss."""
    try:
        key = _prompt_s3_key(tenant_id)
        resp = s3.get_object(
            Bucket=INGEST_BUCKET or CFG.get("resources", {}).get("pipeline_bucket", ""),
            Key=key,
        )
        data = json.loads(resp["Body"].read().decode("utf-8"))
        if data and isinstance(data, dict):
            _init_logger.info(
                "Loaded prompts from S3 cache: tenant=%s, keys=%s",
                tenant_id,
                list(data.keys()),
            )
            return data
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchKey":
            pass  # Cache miss — expected on first run
        else:
            _init_logger.warning("S3 prompt cache read failed for %s: %s", tenant_id, e)
    except Exception as e:
        _init_logger.warning("S3 prompt cache read failed for %s: %s", tenant_id, e)
    return {}


def _save_prompts_to_s3(tenant_id, prompt_content):
    """Save resolved prompt content to S3 for other Lambda instances to reuse."""
    try:
        key = _prompt_s3_key(tenant_id)
        bucket = INGEST_BUCKET or CFG.get("resources", {}).get("pipeline_bucket", "")
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=json.dumps(prompt_content),
            ContentType="application/json",
        )
        _init_logger.info(
            "Saved prompts to S3 cache: tenant=%s, key=%s", tenant_id, key
        )
    except Exception as e:
        _init_logger.warning("S3 prompt cache write failed for %s: %s", tenant_id, e)


def _render_template(template, variables):
    """Replace {{var}} placeholders in a prompt template."""
    result = template
    for k, v in variables.items():
        result = result.replace("{{" + k + "}}", v)
    return result


def converse_with_cache(
    prompt_key, prompt_variables, guardrail_config=None, max_tokens_override=None
):
    """Call Converse API with prompt caching using direct model ID.
    Falls back to managed prompt ARN if prompt content wasn't resolved.
    If guardrail throttles, retries immediately without guardrail.

    Args:
        prompt_key: Key identifying the prompt (e.g., 'enrichment', 'classification')
        prompt_variables: Dict of variables to render in the prompt template
        guardrail_config: Optional guardrail configuration
        max_tokens_override: Optional override for max_tokens (used for retry on truncation)
    """
    content = PROMPT_CONTENT.get(prompt_key)
    if not content or not LLM_MODEL_ID:
        kwargs = {
            "modelId": PROMPT_ARNS[prompt_key],
            "promptVariables": {k: {"text": v} for k, v in prompt_variables.items()},
        }
        if guardrail_config:
            kwargs["guardrailConfig"] = guardrail_config
        try:
            return _converse_with_timeout(kwargs, BEDROCK_CALL_TIMEOUT)
        except ClientError as e:
            if "ThrottlingException" in str(e) and guardrail_config:
                log.warn(
                    "Guardrail throttled, retrying without guardrail",
                    prompt_key=prompt_key,
                )
                del kwargs["guardrailConfig"]
                return _converse_with_timeout(kwargs, BEDROCK_CALL_TIMEOUT)
            raise

    user_text = _render_template(content["user"], prompt_variables)
    inf_cfg = CFG.get("inference", {}).get(prompt_key, {})

    # Use override if provided, otherwise use config value
    max_tokens = (
        max_tokens_override
        if max_tokens_override is not None
        else inf_cfg.get("max_tokens", 4096)
    )

    kwargs = {
        "modelId": LLM_MODEL_ID,
        "system": [
            {"text": content["system"]},
        ],
        "messages": [{"role": "user", "content": [{"text": user_text}]}],
        "inferenceConfig": {
            "temperature": inf_cfg.get("temperature", 0),
            "maxTokens": max_tokens,
        },
    }
    if guardrail_config:
        kwargs["guardrailConfig"] = guardrail_config
    try:
        return _converse_with_timeout(kwargs, BEDROCK_CALL_TIMEOUT)
    except ClientError as e:
        if "ThrottlingException" in str(e) and guardrail_config:
            log.warn(
                "Guardrail throttled, retrying without guardrail", prompt_key=prompt_key
            )
            del kwargs["guardrailConfig"]
            return _converse_with_timeout(kwargs, BEDROCK_CALL_TIMEOUT)
        raise


MAX_WORKERS = CFG.get("pipeline", {}).get("max_workers_phase2", 5)
THROTTLE_BASE_DELAY = CFG.get("pipeline", {}).get("throttle_base_delay", 5.0)
THROTTLE_MAX_RETRIES = CFG.get("pipeline", {}).get("throttle_max_retries", 5)

COSINE_DISTANCE_THRESHOLD = CFG.get("dedup", {}).get("cosine_distance_threshold", 0.05)
TOP_K = CFG.get("dedup", {}).get("top_k", 5)
QUALITY_REFERENCE_TOP_K = CFG.get("dedup", {}).get("quality_reference_top_k", 2)

QUALITY_THRESHOLD = CFG.get("quality_thresholds", {})
DEFAULT_QUALITY_THRESHOLD = CFG.get("quality_thresholds", {}).get("default", 70)

# ── Structured logger and token tracker (module-level for warm start reuse) ───
log = get_logger(tenant_id=TENANT_ID, lambda_name="dedup")
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
    global CFG, TABLE_NAME, INGEST_BUCKET, VECTOR_BUCKET, VECTOR_INDEX
    global TENANT_ID, PROMPT_ARNS, PROMPT_CONTENT, LLM_MODEL_ID
    global MAX_WORKERS, THROTTLE_BASE_DELAY, THROTTLE_MAX_RETRIES
    global COSINE_DISTANCE_THRESHOLD, TOP_K, QUALITY_REFERENCE_TOP_K
    global QUALITY_THRESHOLD, DEFAULT_QUALITY_THRESHOLD
    global GUARDRAIL_CONFIG, log, token_tracker
    CFG = load_config(tenant_code=tenant_code, home_region=home_region)
    TABLE_NAME = CFG["resources"]["table_name"]
    INGEST_BUCKET = CFG["resources"]["pipeline_bucket"]
    VECTOR_BUCKET = CFG["resources"]["vector_bucket"]
    VECTOR_INDEX = CFG["resources"]["vector_index"]
    # Use UUID from event if available, otherwise fall back to AppConfig value
    TENANT_ID = tenant_uuid or CFG["resources"]["tenant_id"]

    # Validate required configuration from AppConfig
    if not TABLE_NAME:
        raise ValueError("table_name is required in AppConfig resources")
    if not INGEST_BUCKET:
        raise ValueError("pipeline_bucket is required in AppConfig resources")
    if not VECTOR_BUCKET:
        raise ValueError("vector_bucket is required in AppConfig resources")
    if not VECTOR_INDEX:
        raise ValueError("vector_index is required in AppConfig resources")
    if not TENANT_ID:
        raise ValueError("tenant_id is required in AppConfig resources")

    PROMPT_ARNS = CFG.get("prompts", {})
    LLM_MODEL_ID = CFG.get("models", {}).get("llm_model_id", "")

    # Reuse cached prompt content for the same tenant (warm Lambda reuse).
    # Avoids redundant GetPrompt API calls that cause throttling at scale.
    # Priority: in-memory cache → S3 cache → GetPrompt API
    # S3 paths use TENANT_ID (UUID) for consistency with all other pipeline S3 paths.
    if tenant_code in _prompt_cache:
        PROMPT_CONTENT = _prompt_cache[tenant_code]
    else:
        PROMPT_CONTENT = _load_prompts_from_s3(TENANT_ID)
        if not PROMPT_CONTENT:
            PROMPT_CONTENT = {}
            for _k, _a in PROMPT_ARNS.items():
                _r = _resolve_prompt(_k, _a)
                if _r:
                    PROMPT_CONTENT[_k] = _r
            if PROMPT_CONTENT:
                _save_prompts_to_s3(TENANT_ID, PROMPT_CONTENT)
        _prompt_cache[tenant_code] = PROMPT_CONTENT
    MAX_WORKERS = CFG["pipeline"]["max_workers_phase2"]
    THROTTLE_BASE_DELAY = CFG["pipeline"]["throttle_base_delay"]
    THROTTLE_MAX_RETRIES = CFG["pipeline"]["throttle_max_retries"]
    COSINE_DISTANCE_THRESHOLD = CFG["dedup"]["cosine_distance_threshold"]
    TOP_K = CFG["dedup"]["top_k"]
    QUALITY_REFERENCE_TOP_K = CFG["dedup"]["quality_reference_top_k"]
    QUALITY_THRESHOLD = CFG["quality_thresholds"]
    DEFAULT_QUALITY_THRESHOLD = CFG["quality_thresholds"].get("default", 70)
    log = get_logger(tenant_id=TENANT_ID, lambda_name="dedup")
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


def floats_to_decimals(obj):
    """Recursively convert float values to Decimal for DynamoDB compatibility."""
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: floats_to_decimals(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [floats_to_decimals(i) for i in obj]
    return obj


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

    result_text = response["output"]["message"]["content"][0]["text"].strip()

    if result_text.startswith("```"):
        result_text = result_text.split("```")[1]
        if result_text.startswith("json"):
            result_text = result_text[4:]
        result_text = result_text.strip()

    json_match = re.search(r"\{.*\}", result_text, re.DOTALL)
    if json_match:
        result_text = json_match.group(0)

    return result_text


# Per-classification quality criteria
QUALITY_CRITERIA = {
    "SOP": """- Clear title describing the procedure
- Numbered step-by-step instructions
- Each step is actionable and unambiguous
- Prerequisites or requirements listed if applicable
- Expected outcome stated
- No missing steps or logical gaps""",
    "FAQ": """- Question is clearly stated
- Answer is complete and directly addresses the question
- No jargon without explanation
- Concise but thorough
- Accurate and up to date""",
    "Troubleshooting": """- Problem/symptom clearly described
- Diagnostic steps are logical and ordered
- Resolution steps are clear and actionable
- Common causes identified
- Escalation path provided if issue cannot be resolved""",
    "RCA": """- Incident clearly described with timeline
- Root cause identified and explained
- Contributing factors listed
- Corrective actions defined
- Preventive measures included""",
    "Runbook": """- Purpose and scope clearly stated
- Prerequisites listed
- Step-by-step operational instructions
- Expected outputs or success criteria defined
- Rollback or recovery steps included""",
}


# ── Dedup helpers ─────────────────────────────────────────────────────────────


def _compute_freshness_score(article_id):
    """Compute freshness score (1-10) from article dates in DynamoDB.
    - Updated within 30 days → 10
    - Updated within 90 days → 8
    - Updated within 180 days → 6
    - Updated within 1 year → 4
    - Older than 1 year → 2
    - Problem Finder articles (no dates) → 10 (brand new)
    """
    table = dynamodb.Table(TABLE_NAME)
    try:
        resp = table.get_item(
            Key={"tenant_id": TENANT_ID, "src_kb_article_id": article_id},
            ProjectionExpression="last_updated_ts_utc, created_ts_utc, source_system",
        )
        item = resp.get("Item", {})
    except Exception:
        return 5  # default if lookup fails

    # Problem Finder articles are brand new
    if item.get("source_system", "").startswith("CONTENT_GENERATOR"):
        return 10

    # Use last_updated_ts_utc first, fall back to created_ts_utc
    date_str = item.get("last_updated_ts_utc", "") or item.get("created_ts_utc", "")
    if not date_str:
        return 5  # no date info

    parsed = _parse_freshness_date(date_str)
    if not parsed:
        log.warn(
            "Unparseable freshness date, using default score",
            article_id=article_id,
            date_str=date_str,
        )
        return 5

    try:
        days_old = (datetime.now(timezone.utc) - parsed).days
    except Exception as e:
        log.warn(
            "Freshness date comparison failed, using default score",
            article_id=article_id,
            date_str=date_str,
            error=str(e),
        )
        return 5
    if days_old <= 30:
        return 10
    elif days_old <= 90:
        return 8
    elif days_old <= 180:
        return 6
    elif days_old <= 365:
        return 4
    else:
        return 2


def _parse_freshness_date(date_str):
    """Parse a date string into a timezone-aware datetime. Handles multiple formats.
    Returns None if unparseable.
    """
    if not date_str:
        return None
    for fmt in (
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S%z",
    ):
        try:
            parsed = datetime.strptime(date_str, fmt)
            # Ensure timezone-aware (assume UTC if naive)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except ValueError:
            continue
    return None


def _is_fresher(article_a, article_b):
    """Determine if article_a is fresher than article_b.
    Primary: last_updated_ts_utc (more recent wins).
    Tiebreaker: created_ts_utc (more recent wins).
    Returns True if article_a is fresher.
    """
    upd_a = _parse_freshness_date(article_a.get("last_updated_ts_utc", ""))
    upd_b = _parse_freshness_date(article_b.get("last_updated_ts_utc", ""))

    # If both have last_updated dates, compare them
    if upd_a and upd_b:
        if upd_a != upd_b:
            return upd_a > upd_b
        # Tiebreaker: created_ts_utc
        cre_a = _parse_freshness_date(article_a.get("created_ts_utc", ""))
        cre_b = _parse_freshness_date(article_b.get("created_ts_utc", ""))
        if cre_a and cre_b:
            return cre_a > cre_b
        return False

    # If only one has last_updated, that one is fresher
    if upd_a and not upd_b:
        return True
    if upd_b and not upd_a:
        return False

    # Neither has last_updated — fall back to created_ts_utc
    cre_a = _parse_freshness_date(article_a.get("created_ts_utc", ""))
    cre_b = _parse_freshness_date(article_b.get("created_ts_utc", ""))
    if cre_a and cre_b:
        return cre_a > cre_b

    # No freshness data available — don't swap
    return False


def _resolve_freshness(table, unique_articles, marked_as_duplicate, now, job_id):
    """After dedup, scan duplicate groups and swap UNIQUE/DUPLICATE based on freshness.

    For each UNIQUE article, find all its DUPLICATE siblings (articles where
    duplicate_of == this article_id). Compare freshness dates. If a DUPLICATE
    sibling is fresher, promote it to UNIQUE and demote the current UNIQUE to
    DUPLICATE.

    Returns:
        (updated_unique_articles list, swap_count int)
    """
    swap_count = 0
    unique_by_id = {a["src_kb_article_id"]: a for a in unique_articles}

    for unique_art in list(unique_articles):
        uid = unique_art["src_kb_article_id"]

        # Find all DUPLICATE siblings pointing to this UNIQUE article
        try:
            response = table.query(
                IndexName="duplicate-of-index",
                KeyConditionExpression=(
                    boto3.dynamodb.conditions.Key("tenant_id").eq(TENANT_ID)
                    & boto3.dynamodb.conditions.Key("duplicate_of").eq(uid)
                ),
            )
            siblings = response.get("Items", [])
        except Exception as e:
            log.warn("Freshness query failed", article_id=uid, error=str(e))
            continue

        if not siblings:
            continue

        # Get the UNIQUE article's full record for freshness comparison
        try:
            unique_record = table.get_item(
                Key={"tenant_id": TENANT_ID, "src_kb_article_id": uid}
            ).get("Item", {})
        except Exception:
            continue

        # Find the freshest sibling
        freshest_sibling = None
        for sib in siblings:
            if sib.get("pipeline_status") != "DUPLICATE":
                continue
            if _is_fresher(sib, unique_record):
                if freshest_sibling is None or _is_fresher(sib, freshest_sibling):
                    freshest_sibling = sib

        if not freshest_sibling:
            continue

        # Swap: demote current UNIQUE → DUPLICATE, promote freshest sibling → UNIQUE
        new_unique_id = freshest_sibling["src_kb_article_id"]
        log.info(
            "Freshness swap",
            demoting=uid,
            promoting=new_unique_id,
            old_updated=unique_record.get("last_updated_ts_utc", ""),
            new_updated=freshest_sibling.get("last_updated_ts_utc", ""),
        )

        # Demote old UNIQUE to DUPLICATE
        table.update_item(
            Key={"tenant_id": TENANT_ID, "src_kb_article_id": uid},
            UpdateExpression=(
                "SET is_duplicate = :t, duplicate_of = :d,"
                " #st = :status, updated_at = :now, job_id = :job_id"
            ),
            ExpressionAttributeNames={"#st": "pipeline_status"},
            ExpressionAttributeValues={
                ":t": True,
                ":d": new_unique_id,
                ":status": "DUPLICATE",
                ":now": now,
                ":job_id": job_id,
            },
        )
        try:
            old_embedding = get_vector(uid)
            update_vector_metadata(
                uid,
                old_embedding,
                unique_record.get("classification", ""),
                unique_record.get("source_system", "ITSM_KB"),
                "DUPLICATE",
            )
        except Exception as e:
            log.warn(
                "Failed to update vector metadata for demoted article",
                article_id=uid,
                error=str(e),
            )

        # Promote freshest sibling to UNIQUE
        table.update_item(
            Key={"tenant_id": TENANT_ID, "src_kb_article_id": new_unique_id},
            UpdateExpression=(
                "SET is_duplicate = :f,"
                " #st = :status, updated_at = :now, job_id = :job_id"
                " REMOVE duplicate_of"
            ),
            ExpressionAttributeNames={"#st": "pipeline_status"},
            ExpressionAttributeValues={
                ":f": False,
                ":status": "UNIQUE",
                ":now": now,
                ":job_id": job_id,
            },
        )
        try:
            new_embedding = get_vector(new_unique_id)
            update_vector_metadata(
                new_unique_id,
                new_embedding,
                freshest_sibling.get("classification", ""),
                freshest_sibling.get("source_system", "ITSM_KB"),
                "UNIQUE",
            )
        except Exception as e:
            log.warn(
                "Failed to update vector metadata for promoted article",
                article_id=new_unique_id,
                error=str(e),
            )

        # Update all other siblings to point to the new UNIQUE
        for sib in siblings:
            if sib["src_kb_article_id"] == new_unique_id:
                continue
            table.update_item(
                Key={
                    "tenant_id": TENANT_ID,
                    "src_kb_article_id": sib["src_kb_article_id"],
                },
                UpdateExpression="SET duplicate_of = :d, updated_at = :now",
                ExpressionAttributeValues={":d": new_unique_id, ":now": now},
            )

        # Update unique_articles list: remove old, add new
        unique_articles = [a for a in unique_articles if a["src_kb_article_id"] != uid]
        try:
            new_embedding = get_vector(new_unique_id)
            unique_articles.append(
                {
                    "src_kb_article_id": new_unique_id,
                    "classification": freshest_sibling.get("classification", ""),
                    "source_system": freshest_sibling.get("source_system", "ITSM_KB"),
                    "source_file_path": freshest_sibling.get("source_file_path", ""),
                    "embedding": new_embedding,
                }
            )
        except Exception as e:
            log.error(
                "Failed to add promoted article to quality pass",
                article_id=new_unique_id,
                error=str(e),
            )

        marked_as_duplicate.add(uid)
        marked_as_duplicate.discard(new_unique_id)
        swap_count += 1

    return unique_articles, swap_count


def get_embedded_articles(table):
    """Query DynamoDB for articles with status=EMBEDDED (paginated)."""
    articles = []
    kwargs = {
        "IndexName": "pipeline-status-index",
        "KeyConditionExpression": boto3.dynamodb.conditions.Key("tenant_id").eq(
            TENANT_ID
        )
        & boto3.dynamodb.conditions.Key("pipeline_status").eq("EMBEDDED"),
    }
    while True:
        response = table.query(**kwargs)
        articles.extend(response.get("Items", []))
        if "LastEvaluatedKey" not in response:
            break
        kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    log.info("Fetched EMBEDDED articles", count=len(articles))
    return articles


def get_unique_pending_quality(table):
    """Query DynamoDB for UNIQUE articles that haven't been quality-checked yet (paginated)."""
    articles = []
    kwargs = {
        "IndexName": "pipeline-status-index",
        "KeyConditionExpression": boto3.dynamodb.conditions.Key("tenant_id").eq(
            TENANT_ID
        )
        & boto3.dynamodb.conditions.Key("pipeline_status").eq("UNIQUE"),
    }
    while True:
        response = table.query(**kwargs)
        articles.extend(response.get("Items", []))
        if "LastEvaluatedKey" not in response:
            break
        kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    pending = [a for a in articles if a.get("pre_final_score") is None]
    log.info("Fetched UNIQUE pending quality articles", count=len(pending))
    return pending


def get_vector(article_id):
    """Fetch the vector embedding for an article from S3 Vectors."""
    response = s3vectors.get_vectors(
        vectorBucketName=VECTOR_BUCKET,
        indexName=VECTOR_INDEX,
        keys=[article_id],
        returnData=True,
    )
    vectors = response.get("vectors", [])
    if not vectors:
        raise ValueError(f"No vector found for article_id={article_id}")
    return vectors[0]["data"]["float32"]


def query_similar(article_id, embedding):
    """Query S3 Vectors for top-K similar articles (UNIQUE or PENDING), excluding self."""
    response = s3vectors.query_vectors(
        vectorBucketName=VECTOR_BUCKET,
        indexName=VECTOR_INDEX,
        queryVector={"float32": embedding},
        topK=TOP_K + 1,
        returnMetadata=True,
        returnDistance=True,
        filter={
            "$and": [
                {"tenant_id": {"$eq": TENANT_ID}},
                {"status": {"$in": ["UNIQUE", "PENDING"]}},
            ]
        },
    )
    raw_vectors = response.get("vectors", [])
    log.debug(
        "Query similar raw results",
        article_id=article_id,
        matches=[v["key"] for v in raw_vectors],
    )
    return [v for v in raw_vectors if v["key"] != article_id]


def update_vector_metadata(
    article_id, embedding, classification, source_system, status
):
    """Update S3 Vectors metadata — requires re-put since no update API exists."""
    s3vectors.put_vectors(
        vectorBucketName=VECTOR_BUCKET,
        indexName=VECTOR_INDEX,
        vectors=[
            {
                "key": article_id,
                "data": {"float32": embedding},
                "metadata": {
                    "tenant_id": TENANT_ID,
                    "src_kb_article_id": article_id,
                    "status": status,
                    "classification": classification,
                    "source_system": source_system,
                },
            }
        ],
    )


# ── Quality validation helpers ────────────────────────────────────────────────


def fetch_reference_articles(article_id, embedding, classification):
    """Fetch top nearest UNIQUE ITSM_KB articles of the same classification as quality references."""
    try:
        response = s3vectors.query_vectors(
            vectorBucketName=VECTOR_BUCKET,
            indexName=VECTOR_INDEX,
            queryVector={"float32": embedding},
            topK=QUALITY_REFERENCE_TOP_K + 1,
            returnMetadata=True,
            returnDistance=False,
            filter={
                "$and": [
                    {"tenant_id": {"$eq": TENANT_ID}},
                    {"status": {"$eq": "UNIQUE"}},
                    {"source_system": {"$eq": "ITSM_KB"}},
                    {"classification": {"$eq": classification}},
                ]
            },
        )
        candidates = [v for v in response.get("vectors", []) if v["key"] != article_id]
        candidates = candidates[:QUALITY_REFERENCE_TOP_K]
    except Exception as e:
        log.warn(
            "Failed to fetch reference vectors", article_id=article_id, error=str(e)
        )
        return []

    if not candidates:
        return []

    table = dynamodb.Table(TABLE_NAME)
    references = []
    for candidate in candidates:
        ref_id = candidate["key"]
        try:
            resp = table.get_item(
                Key={"tenant_id": TENANT_ID, "src_kb_article_id": ref_id}
            )
            item = resp.get("Item")
            if not item:
                continue

            # Parse S3 URI to extract bucket and key
            ref_source_path = item["source_file_path"]
            if ref_source_path.startswith("s3://"):
                s3_uri_parts = ref_source_path.replace("s3://", "").split("/", 1)
                ref_bucket = s3_uri_parts[0]
                s3_key = unquote_plus(s3_uri_parts[1]) if len(s3_uri_parts) > 1 else ""
            else:
                ref_bucket = INGEST_BUCKET
                s3_key = unquote_plus(ref_source_path)

            text = extract_text_from_s3(ref_bucket, s3_key)
            references.append((ref_id, text))
        except Exception as e:
            log.warn(
                "Failed to fetch reference article",
                article_id=article_id,
                ref_id=ref_id,
                error=str(e),
            )

    return references


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
            from markup_tools import get_article_content

            html_content, _ = get_article_content(data)
            if not html_content.strip():
                log.warn("No text content found in JSON", bucket=bucket, key=key)
                return ""
            plain_text = extract_plain_text(html_content)
            log.info("Extracted text from JSON", key=key, text_length=len(plain_text))
            return plain_text
        except json.JSONDecodeError as e:
            log.error("JSON decode error", bucket=bucket, key=key, error=str(e))
            raise
    else:
        # Assume it's HTML
        plain_text = extract_plain_text(content)
        log.info("Extracted text from HTML", key=key, text_length=len(plain_text))
        return plain_text


def run_quality_check(
    article_id,
    article_text,
    classification,
    source_system,
    embedding,
    freshness_score=None,
):
    """Invoke Claude Sonnet 4.5 to validate article quality."""
    criteria = QUALITY_CRITERIA.get(
        classification, "- Clear, complete, accurate and actionable content"
    )
    threshold = QUALITY_THRESHOLD.get(source_system, DEFAULT_QUALITY_THRESHOLD)
    max_chars = CFG["truncation"]["quality_max_chars"]

    def sanitise(text):
        return text.replace("{", "(").replace("}", ")").replace("\\", " ")

    references = fetch_reference_articles(article_id, embedding, classification)

    if references:
        ref_sections = []
        for i, (ref_id, ref_text) in enumerate(references, 1):
            ref_sections.append(f"### Reference {i} ({ref_id})\n{sanitise(ref_text)}")
        references_block = "\n\n".join(ref_sections)
        log.info(
            "Using reference articles for quality check",
            article_id=article_id,
            ref_count=len(references),
        )
    else:
        references_block = "No reference articles available for this classification."
        log.info(
            "No reference articles found, standalone evaluation", article_id=article_id
        )

    response = invoke_with_retry(
        converse_with_cache,
        prompt_key="quality",
        prompt_variables={
            "classification": classification,
            "references": references_block,
            "article_content": sanitise(article_text[:max_chars]),
            "criteria": criteria,
        },
        guardrail_config=GUARDRAIL_CONFIG if GUARDRAIL_CONFIG else None,
    )

    result_text = parse_converse_response(response, "quality", article_id)
    log.debug(
        "Quality raw response",
        article_id=article_id,
        response_preview=result_text[:300],
    )

    result = None
    try:
        result = json.loads(result_text)
    except json.JSONDecodeError:
        json_match2 = re.search(r"\{.*\}", result_text, re.DOTALL)
        if json_match2:
            try:
                result = json.loads(json_match2.group(0))
            except json.JSONDecodeError:
                pass

    if result is None:
        score_match = re.search(r'"quality_score"\s*:\s*(\d+)', result_text)
        quality_score = int(score_match.group(1)) if score_match else 50
        log.warn(
            "Quality JSON parse failed, extracted score via regex",
            article_id=article_id,
            extracted_score=quality_score,
        )
        result = {
            "quality_score": quality_score,
            "issues": [],
            "feedback": "JSON parse error — score extracted via regex",
        }

    # Validate and sanitise LLM response before using
    dim_config = CFG.get("quality_dimensions", {})
    active_dims = {k: v for k, v in dim_config.items() if v.get("weight", 0) > 0}
    try:
        result = validate_quality_response(result, list(active_dims.keys()))
    except ValidationError as ve:
        log.error(
            "Quality response validation failed", article_id=article_id, error=str(ve)
        )
        result = {
            "quality_score": 0,
            "quality_scores": {d: 0 for d in active_dims},
            "issues": [],
            "feedback": f"Validation error: {ve}",
        }

    quality_score = result["quality_score"]
    quality_passed = quality_score >= threshold

    quality_scores = result[
        "quality_scores"
    ]  # Already validated by validate_quality_response

    # Inject freshness score (computed from dates, not LLM)
    if "freshness" in active_dims and freshness_score is not None:
        quality_scores["freshness"] = freshness_score

    # Compute weighted dimension score (0-100)
    total_weight = sum(v["weight"] for v in active_dims.values())
    if total_weight > 0 and quality_scores:
        weighted_sum = sum(
            quality_scores[d] * active_dims[d]["weight"] for d in quality_scores
        )
        dimension_score = round((weighted_sum / total_weight) * 10, 1)
    else:
        dimension_score = quality_score

    # Final score = average of weighted dimension score and overall criteria-based score
    final_score = round((dimension_score + quality_score) / 2, 1)

    return {
        "final_score": final_score,
        "criteria_score": quality_score,
        "dimension_score": dimension_score,
        "quality_passed": final_score >= threshold,
        "scores": quality_scores,
        "issues": result.get("issues", []),
        "feedback": result.get("feedback", ""),
        "threshold_applied": threshold,
        "active_dimensions": list(active_dims.keys()),
    }


# ── Enrichment (used for all articles — passed and failed) ────────────────────


def run_retirement_detection(
    article_id, text, classification, last_updated_ts_utc, created_ts_utc=None
):
    """Check if article references outdated technology.
    Returns dict with LLM decision or None if retirement detection is disabled or article is too young.
    """
    cfg_retirement = CFG.get("retirement", {})
    if not cfg_retirement.get("enabled", False):
        log.debug("Retirement detection disabled", article_id=article_id)
        return None

    # Pre-check: Only run LLM if article is older than threshold
    llm_age_threshold_days = cfg_retirement.get(
        "llm_age_threshold_days", 1095
    )  # Default 3 years
    article_date_str = created_ts_utc or last_updated_ts_utc

    if article_date_str:
        article_date = _parse_freshness_date(article_date_str)
        if article_date:
            age_days = (datetime.now(timezone.utc) - article_date).days
            if age_days < llm_age_threshold_days:
                log.info(
                    "Skipping retirement LLM check - article too young",
                    article_id=article_id,
                    age_days=age_days,
                    threshold_days=llm_age_threshold_days,
                )
                return None
            log.debug(
                "Article age check passed",
                article_id=article_id,
                age_days=age_days,
                threshold_days=llm_age_threshold_days,
            )
        else:
            log.warn(
                "Could not parse article date for age check",
                article_id=article_id,
                date_str=article_date_str,
            )
    else:
        log.warn("No article date available for age check", article_id=article_id)

    max_chars = CFG["truncation"].get("retirement_detection_max_chars", 50000)
    truncated = text[:max_chars]

    log.info(
        "Running retirement detection",
        article_id=article_id,
        classification=classification,
        text_chars=len(text),
        truncated_chars=len(truncated),
        last_updated=last_updated_ts_utc,
    )

    try:
        response = invoke_with_retry(
            converse_with_cache,
            prompt_key="retirement_detection",
            prompt_variables={
                "classification": classification,
                "article_content": truncated,
            },
            guardrail_config=None,  # No guardrail for retirement detection
        )

        result_text = parse_converse_response(response, "retirement", article_id)
        log.debug(
            "Retirement detection raw response",
            article_id=article_id,
            response_preview=result_text[:300],
        )

        result = json.loads(result_text)

        # Validate response structure
        if not isinstance(result, dict):
            log.error("Retirement detection returned non-dict", article_id=article_id)
            return None

        is_outdated = result.get("is_outdated", False)
        reason = result.get("reason", "")
        confidence = result.get("confidence", 0)

        # Validate types
        if not isinstance(is_outdated, bool):
            is_outdated = bool(is_outdated)
        if not isinstance(confidence, (int, float)):
            confidence = 0
        confidence = int(confidence)

        log.info(
            "Retirement detection complete",
            article_id=article_id,
            is_outdated=is_outdated,
            confidence=confidence,
            reason=reason[:100],
        )

        return {
            "retirement_llm_decision": is_outdated,
            "retirement_llm_reason": reason[:500],  # Truncate to 500 chars
            "retirement_llm_confidence": confidence,
        }

    except json.JSONDecodeError as e:
        log.error(
            "Retirement detection JSON parse failed",
            article_id=article_id,
            error=str(e),
            response_preview=result_text[:500] if "result_text" in locals() else "N/A",
        )
        return None
    except Exception as e:
        log.error("Retirement detection failed", article_id=article_id, error=str(e))
        return None


def compute_retirement_flag(
    article_id, last_updated_ts_utc, created_ts_utc, llm_result
):
    """Combine freshness age + LLM decision into a single retirement flag.
    Uses last_updated_ts_utc, falls back to created_ts_utc if empty.

    Returns:
        tuple: (retirement_flag, age_days, date_used)
            retirement_flag: 'BOTH_FLAGGED' | 'FRESHNESS_FLAGGED' | 'LLM_FLAGGED' | 'NOT_FLAGGED'
            age_days: int - days since last update or creation
            date_used: str - which date field was used ('last_updated' or 'created')
    """
    cfg_retirement = CFG.get("retirement", {})
    threshold_days = cfg_retirement.get("freshness_threshold_days", 1825)  # 5 years
    min_confidence = cfg_retirement.get("min_confidence_threshold", 90)

    # Use last_updated_ts_utc first, fall back to created_ts_utc
    date_str = last_updated_ts_utc or created_ts_utc
    date_used = "last_updated" if last_updated_ts_utc else "created"

    parsed_date = _parse_freshness_date(date_str)
    if not parsed_date:
        log.warn(
            "Could not parse date for retirement check",
            article_id=article_id,
            last_updated_ts_utc=last_updated_ts_utc,
            created_ts_utc=created_ts_utc,
        )
        age_days = 0
    else:
        try:
            age_days = (datetime.now(timezone.utc) - parsed_date).days
        except Exception as e:
            log.warn(
                "Failed to compute age_days for retirement",
                article_id=article_id,
                error=str(e),
            )
            age_days = 0

    # Check freshness flag
    freshness_flagged = age_days > threshold_days

    # Check LLM flag (only if confidence > threshold, not >=)
    llm_flagged = False
    if llm_result:
        is_outdated = llm_result.get("retirement_llm_decision", False)
        confidence = llm_result.get("retirement_llm_confidence", 0)
        # CRITICAL: confidence must be STRICTLY GREATER than threshold (> not >=)
        llm_flagged = is_outdated and confidence > min_confidence

    # Determine final flag
    if freshness_flagged and llm_flagged:
        flag = "BOTH_FLAGGED"
    elif freshness_flagged:
        flag = "FRESHNESS_FLAGGED"
    elif llm_flagged:
        flag = "LLM_FLAGGED"
    else:
        flag = "NOT_FLAGGED"

    log.info(
        "Retirement flag computed",
        article_id=article_id,
        flag=flag,
        age_days=age_days,
        date_used=date_used,
        freshness_flagged=freshness_flagged,
        llm_flagged=llm_flagged,
        threshold_days=threshold_days,
        min_confidence=min_confidence,
    )

    return flag, age_days


# ── Enrichment (used for all articles — passed and failed) ────────────────────


def run_enrichment(
    article_id,
    html_content,
    classification,
    quality_issues,
    quality_scores,
    pre_score=0,
    source_system="ITSM_KB",
    max_tokens_override=None,
):
    """Enrich article by sending HTML with media placeholders to LLM.
    LLM returns enriched HTML directly. Placeholders are restored after.
    If max_tokens_override is provided (from previous truncation), uses that instead of config default.
    On truncation: raises ValueError with token info so caller can reset to RAW for next run.
    """
    from markup_tools import extract_placeholders, restore_placeholders

    # Pick enrichment prompt: use per-source-system quality threshold
    threshold = QUALITY_THRESHOLD.get(source_system, DEFAULT_QUALITY_THRESHOLD)
    if pre_score >= threshold and "enrichment_light" in PROMPT_ARNS:
        enrichment_mode = "light"
    else:
        enrichment_mode = "deep"

    def unwrap(issue):
        if isinstance(issue, dict):
            return issue.get("S", str(issue))
        return str(issue)

    def sanitise(text):
        return text.replace("{", "(").replace("}", ")").replace("\\", " ")

    clean_issues = [unwrap(i) for i in quality_issues] if quality_issues else []
    issues_text = (
        "\n".join([f"- {sanitise(i)}" for i in clean_issues])
        if clean_issues
        else "No specific issues identified."
    )
    scores_text = (
        "\n".join([f"- {k}: {v}/10" for k, v in quality_scores.items()])
        if quality_scores
        else "No dimension scores available."
    )

    # Extract media/links as placeholders
    html_with_placeholders, placeholder_map = extract_placeholders(html_content)

    # Calculate input size metrics for analysis
    input_html_chars = len(html_with_placeholders)
    input_html_words = len(html_with_placeholders.split())

    log.info(
        "Enrichment start",
        article_id=article_id,
        mode=enrichment_mode,
        pre_score=pre_score,
        placeholders=len(placeholder_map),
        html_chars=input_html_chars,
        html_words=input_html_words,
        source_system=source_system,
    )

    # Prepare prompt variables (reused for retry)
    prompt_key = "enrichment_light" if enrichment_mode == "light" else "enrichment"
    prompt_variables = {
        "classification": classification,
        "scores": scores_text,
        "issues": issues_text,
        "article_html": sanitise(html_with_placeholders),
    }

    # Get configured max_tokens for this prompt
    inf_cfg = CFG.get("inference", {}).get(prompt_key, {})
    configured_max_tokens = inf_cfg.get("max_tokens", 8192)

    # Use override from caller (doubled from previous truncation) or default
    effective_max_tokens = (
        max_tokens_override if max_tokens_override else configured_max_tokens
    )

    # Single attempt — no retry loop
    response = None
    result_text = None

    log.info(
        "Enrichment LLM call",
        article_id=article_id,
        max_tokens=effective_max_tokens,
        prompt_key=prompt_key,
        using_override=max_tokens_override is not None,
    )

    # Single LLM call with full HTML
    response = invoke_with_retry(
        converse_with_cache,
        prompt_key=prompt_key,
        prompt_variables=prompt_variables,
        guardrail_config=GUARDRAIL_CONFIG if GUARDRAIL_CONFIG else None,
        max_tokens_override=effective_max_tokens,
    )

    # Get raw response text — don't use parse_converse_response's JSON extraction
    # because HTML inside JSON breaks the regex
    usage = response.get("usage", {})
    input_tokens = usage.get("inputTokens", 0)
    output_tokens = usage.get("outputTokens", 0)

    token_tracker.record(
        "enrich",
        article_id,
        input_tokens,
        output_tokens,
    )

    stop_reason = response.get("stopReason", "")

    # Log detailed token usage for analysis
    log.info(
        "Enrichment LLM response",
        article_id=article_id,
        stop_reason=stop_reason,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        max_tokens=effective_max_tokens,
        tokens_used_pct=(
            round((output_tokens / effective_max_tokens) * 100, 1)
            if effective_max_tokens > 0
            else 0
        ),
    )

    # Check for guardrail intervention
    if stop_reason == "guardrail_intervened":
        trace_info = response.get("trace", {}).get("guardrail", {})
        log.error(
            "Enrichment blocked by guardrail",
            article_id=article_id,
            trace_info=trace_info,
        )
        raise GuardrailBlockedError("enrich", article_id, trace_info)

    # Check for max_tokens truncation
    if stop_reason == "max_tokens":
        log.warn(
            "Enrichment truncated due to max_tokens limit — resetting to RAW for retry with higher tokens",
            article_id=article_id,
            max_tokens=effective_max_tokens,
            output_tokens=output_tokens,
            input_html_chars=input_html_chars,
            input_html_words=input_html_words,
            stop_reason=stop_reason,
            mode=enrichment_mode,
        )
        # Track the token limit that failed so next run can use a higher value
        raise ValueError(
            f"ENRICHMENT_TRUNCATED:max_tokens={effective_max_tokens}:output={output_tokens}:"
            f"article={article_id}"
        )

    # Check for other stop reasons that might indicate issues
    if stop_reason not in ["end_turn", "max_tokens", "guardrail_intervened"]:
        log.warn(
            "Enrichment stopped with unexpected reason",
            article_id=article_id,
            stop_reason=stop_reason,
            output_tokens=output_tokens,
        )

    # Success
    log.info(
        "Enrichment response received successfully",
        article_id=article_id,
        stop_reason=stop_reason,
        output_tokens=output_tokens,
        max_tokens=effective_max_tokens,
    )

    result_text = response["output"]["message"]["content"][0]["text"].strip()
    result_text_length = len(result_text)

    # Strip markdown code blocks if present
    if result_text.startswith("```"):
        log.debug("Stripping markdown code blocks from response", article_id=article_id)
        lines = result_text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        result_text = "\n".join(lines).strip()

    # Parse JSON response — handle HTML inside JSON carefully
    result = None
    parse_method = None

    try:
        result = json.loads(result_text)
        parse_method = "direct_json_loads"
        log.debug(
            "JSON parsed successfully using direct json.loads", article_id=article_id
        )
    except json.JSONDecodeError as e:
        log.debug(
            "Direct JSON parse failed, trying brace-matching parser",
            article_id=article_id,
            error=str(e)[:100],
        )

    # Try finding JSON by matching outermost braces (string-aware)
    if result is None:
        brace_depth = 0
        start = None
        in_string = False
        escape_next = False
        for i, ch in enumerate(result_text):
            if escape_next:
                escape_next = False
                continue
            if ch == "\\":
                escape_next = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                if brace_depth == 0:
                    start = i
                brace_depth += 1
            elif ch == "}":
                brace_depth -= 1
                if brace_depth == 0 and start is not None:
                    try:
                        result = json.loads(result_text[start : i + 1])
                        parse_method = "brace_matching"
                        log.debug(
                            "JSON parsed successfully using brace-matching parser",
                            article_id=article_id,
                            json_start=start,
                            json_end=i + 1,
                        )
                        break
                    except json.JSONDecodeError:
                        start = None

    if result is None:
        log.error(
            "Enrichment JSON parse failed after all attempts",
            article_id=article_id,
            response_length=result_text_length,
            response_preview=result_text[:500],
            response_suffix=result_text[-200:] if len(result_text) > 200 else "",
            stop_reason=stop_reason,
            output_tokens=output_tokens,
        )
        raise ValueError(f"Enrichment JSON parse failed for {article_id}")

    log.debug(
        "JSON parsed successfully", article_id=article_id, parse_method=parse_method
    )

    enriched_html = result.get("enriched_html", "")
    new_sections_html = result.get("new_sections_html", "")
    enrichment_summary = result.get("enrichment_summary", "")

    if not enriched_html:
        log.error(
            "Enrichment returned empty enriched_html",
            article_id=article_id,
            result_keys=list(result.keys()),
            new_sections_present=bool(new_sections_html),
        )
        raise ValueError(f"Enrichment returned empty enriched_html for {article_id}")

    # Restore placeholders
    restored_html, missing = restore_placeholders(enriched_html, placeholder_map)

    if missing:
        log.warn(
            "Placeholders missing from enrichment, used fallback insertion",
            article_id=article_id,
            missing_count=len(missing),
            total_placeholders=len(placeholder_map),
            missing_placeholders=[m["placeholder"] for m in missing][:10],
        )  # Log first 10

    # Append new sections
    if new_sections_html and new_sections_html.strip():
        restored_html = restored_html + "\n" + new_sections_html.strip()
        log.debug(
            "Appended new sections to enriched HTML",
            article_id=article_id,
            new_sections_chars=len(new_sections_html),
        )

    # Calculate output size metrics
    output_html_chars = len(restored_html)
    output_html_words = len(restored_html.split())
    size_increase_pct = (
        round(((output_html_chars - input_html_chars) / input_html_chars) * 100, 1)
        if input_html_chars > 0
        else 0
    )

    log.info(
        "Enrichment complete",
        article_id=article_id,
        mode=enrichment_mode,
        placeholders_total=len(placeholder_map),
        placeholders_missing=len(missing),
        input_chars=input_html_chars,
        output_chars=output_html_chars,
        size_increase_pct=size_increase_pct,
        parse_method=parse_method,
        enrichment_summary=enrichment_summary[:100],
    )

    return {
        "enriched_html": restored_html,
        "enrichment_summary": enrichment_summary or "Editorial improvements applied",
    }


def apply_enrichment(
    bucket, s3_key, article_id, enrichment_result, original_paragraphs, pre_score=0
):
    """Apply enrichment result. The enriched HTML is already complete from run_enrichment.
    Just upload to S3 as the generated file.
    """
    enriched_html = enrichment_result.get("enriched_html", "")
    if not enriched_html:
        return None, False

    return enriched_html.encode("utf-8"), True


# ── Post-enrichment quality scoring ──────────────────────────────────────────


def run_post_enrichment_scoring(
    article_id, enriched_text, classification, original_text=None, freshness_score=None
):
    """Re-score an enriched/rewritten article with original as context for comparison."""
    max_chars = CFG["truncation"]["post_scoring_max_chars"]

    def sanitise(text):
        return text.replace("{", "(").replace("}", ")").replace("\\", " ")

    # Read active dimensions from AppConfig (weight > 0)
    dim_config = CFG.get("quality_dimensions", {})
    active_dims = {k: v for k, v in dim_config.items() if v.get("weight", 0) > 0}

    prompt_vars = {
        "classification": classification,
        "article_content": sanitise(enriched_text[:max_chars]),
    }
    if original_text:
        prompt_vars["original_content"] = sanitise(original_text[:max_chars])

    response = invoke_with_retry(
        converse_with_cache,
        prompt_key="post_scoring",
        prompt_variables=prompt_vars,
        guardrail_config=GUARDRAIL_CONFIG if GUARDRAIL_CONFIG else None,
    )

    result_text = parse_converse_response(response, "post_score", article_id)

    result = None
    try:
        result = json.loads(result_text)
    except json.JSONDecodeError:
        json_match2 = re.search(r"\{.*\}", result_text, re.DOTALL)
        if json_match2:
            try:
                result = json.loads(json_match2.group(0))
            except json.JSONDecodeError:
                pass

    if result is None:
        log.warn("Post-scoring JSON parse failed", article_id=article_id)
        return {
            "final_score": 0,
            "dimension_score": 0,
            "criteria_score": 0,
            "scores": {d: 0 for d in active_dims},
            "active_dimensions": list(active_dims.keys()),
        }

    # Validate and sanitise LLM response
    try:
        result = validate_quality_response(result, list(active_dims.keys()))
    except ValidationError as ve:
        log.error(
            "Post-scoring response validation failed",
            article_id=article_id,
            error=str(ve),
        )
        return {
            "final_score": 0,
            "dimension_score": 0,
            "criteria_score": 0,
            "scores": {d: 0 for d in active_dims},
            "active_dimensions": list(active_dims.keys()),
        }

    quality_score = result["quality_score"]

    quality_scores = result["quality_scores"]  # Already validated

    # Inject freshness score (same value as pre-scoring, computed once)
    if "freshness" in active_dims and freshness_score is not None:
        quality_scores["freshness"] = freshness_score

    # Compute weighted dimension score (0-100), same logic as run_quality_check
    total_weight = sum(v["weight"] for v in active_dims.values())
    if total_weight > 0 and quality_scores:
        weighted_sum = sum(
            quality_scores[d] * active_dims[d]["weight"] for d in quality_scores
        )
        dimension_score = round((weighted_sum / total_weight) * 10, 1)
    else:
        dimension_score = quality_score

    final_score = round((dimension_score + quality_score) / 2, 1)

    post_result = {
        "final_score": final_score,
        "criteria_score": quality_score,
        "dimension_score": dimension_score,
        "scores": quality_scores,
        "active_dimensions": list(active_dims.keys()),
    }
    log.info(
        "Post-enrichment scoring complete",
        article_id=article_id,
        final_score=final_score,
        criteria_score=quality_score,
        dimension_score=dimension_score,
        dimensions=quality_scores,
    )
    return post_result


# ── Lambda handler ────────────────────────────────────────────────────────────


def quality_and_enrich(
    article_id, classification, source_system, source_file_path, embedding, now, job_id
):
    """Run quality check + enrichment for a single UNIQUE article."""
    table = dynamodb.Table(TABLE_NAME)
    result_out = {
        "article_id": article_id,
        "quality_passed": False,
        "enriched": False,
        "enrich_error": None,
    }

    if not source_file_path or source_file_path.strip() == "":
        log.warn(
            "Empty source_file_path, skipping quality/enrichment", article_id=article_id
        )
        table.update_item(
            Key={"tenant_id": TENANT_ID, "src_kb_article_id": article_id},
            UpdateExpression="SET #st = :status, quality_feedback = :qf, job_id = :job_id, updated_at = :now",
            ExpressionAttributeNames={"#st": "pipeline_status"},
            ExpressionAttributeValues={
                ":status": "QUALITY_FAILED",
                ":qf": "SKIPPED: empty source_file_path",
                ":job_id": job_id,
                ":now": now,
            },
        )
        return result_out

    # Parse S3 URI to extract bucket and key (source files can be in different bucket)
    if source_file_path.startswith("s3://"):
        s3_uri_parts = source_file_path.replace("s3://", "").split("/", 1)
        source_bucket = s3_uri_parts[0]
        s3_key = unquote_plus(s3_uri_parts[1]) if len(s3_uri_parts) > 1 else ""
    else:
        # Fallback: assume it's in ingest bucket
        source_bucket = INGEST_BUCKET
        s3_key = unquote_plus(source_file_path)

    if not s3_key or s3_key.strip() == "":
        log.warn(
            "Invalid s3_key from source_file_path",
            article_id=article_id,
            source_file_path=source_file_path,
        )
        table.update_item(
            Key={"tenant_id": TENANT_ID, "src_kb_article_id": article_id},
            UpdateExpression="SET #st = :status, quality_feedback = :qf, job_id = :job_id, updated_at = :now",
            ExpressionAttributeNames={"#st": "pipeline_status"},
            ExpressionAttributeValues={
                ":status": "QUALITY_FAILED",
                ":qf": f"SKIPPED: invalid s3_key from source_file_path={source_file_path}",
                ":job_id": job_id,
                ":now": now,
            },
        )
        return result_out

    # Read article from S3 - handle missing files explicitly
    try:
        article_text = extract_text_from_s3(source_bucket, s3_key)
        s3_resp = s3.get_object(Bucket=source_bucket, Key=s3_key)
        html_content = s3_resp["Body"].read().decode("utf-8")
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchKey":
            log.error(
                "S3 file not found during quality/enrichment - article exists in metadata but file is missing",
                article_id=article_id,
                bucket=source_bucket,
                key=s3_key,
                source_file_path=source_file_path,
            )
            table.update_item(
                Key={"tenant_id": TENANT_ID, "src_kb_article_id": article_id},
                UpdateExpression="SET #st = :status, error_message = :err, job_id = :job_id, updated_at = :now",
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
                "quality_passed": False,
                "enriched": False,
                "enrich_error": None,
                "s3_missing": True,
            }
        else:
            # Re-raise other S3 errors (permissions, throttling, etc.)
            raise

    parsed_paragraphs, _ = parse_html_with_media_map(html_content)
    article_paragraphs = paragraphs_as_text_list(parsed_paragraphs)

    try:
        freshness_score = _compute_freshness_score(article_id)
        quality_result = run_quality_check(
            article_id,
            html_content,
            classification,
            source_system,
            embedding,
            freshness_score=freshness_score,
        )
    except GuardrailBlockedError as gbe:
        log.warn(
            "Article blocked by guardrail during quality check",
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
                ":gr": f"Blocked during {gbe.call_type}: {json.dumps(gbe.trace_info, default=str)[:500]}",
                ":job_id": job_id,
                ":now": now,
            },
        )
        return {
            "article_id": article_id,
            "quality_passed": False,
            "enriched": False,
            "enrich_error": None,
            "guardrail_blocked": True,
        }

    # ── Retirement Detection (after quality, before enrichment) ──────────────
    # Get last_updated_ts_utc, created_ts_utc, and enrichment_summary from DynamoDB
    try:
        article_record = table.get_item(
            Key={"tenant_id": TENANT_ID, "src_kb_article_id": article_id},
            ProjectionExpression="last_updated_ts_utc, created_ts_utc, enrichment_summary",
        ).get("Item", {})
        last_updated_ts_utc = article_record.get("last_updated_ts_utc", "")
        created_ts_utc = article_record.get("created_ts_utc", "")
        prev_enrichment_summary = article_record.get("enrichment_summary", "")
    except Exception as e:
        log.warn(
            "Failed to fetch dates for retirement check",
            article_id=article_id,
            error=str(e),
        )
        last_updated_ts_utc = ""
        created_ts_utc = ""
        prev_enrichment_summary = ""

    retirement_llm_result = run_retirement_detection(
        article_id,
        article_text,
        classification,
        last_updated_ts_utc or created_ts_utc,
        created_ts_utc,
    )
    retirement_flag, retirement_age_days = compute_retirement_flag(
        article_id, last_updated_ts_utc, created_ts_utc, retirement_llm_result
    )

    log.info(
        "Retirement detection complete",
        article_id=article_id,
        retirement_flag=retirement_flag,
        age_days=retirement_age_days,
        llm_decision=(
            retirement_llm_result.get("retirement_llm_decision")
            if retirement_llm_result
            else None
        ),
        llm_confidence=(
            retirement_llm_result.get("retirement_llm_confidence")
            if retirement_llm_result
            else None
        ),
    )

    try:
        quality_status = (
            "QUALITY_PASSED" if quality_result["quality_passed"] else "QUALITY_FAILED"
        )
        log.info(
            "Quality check complete",
            article_id=article_id,
            score=quality_result["final_score"],
            status=quality_status,
        )

        table.update_item(
            Key={"tenant_id": TENANT_ID, "src_kb_article_id": article_id},
            UpdateExpression="SET pre_final_score = :pfs, quality_passed = :qp, quality_feedback = :qf, "
            "quality_issues = :qi, pre_scores = :ps, pre_criteria_score = :pcs, "
            "pre_dimension_score = :pds, quality_checked_at = :now, #st = :status, job_id = :job_id, updated_at = :now, "
            "retirement_flag = :rf, retirement_freshness_days = :rfd, "
            "retirement_llm_decision = :rld, retirement_llm_reason = :rlr, retirement_llm_confidence = :rlc",
            ExpressionAttributeNames={"#st": "pipeline_status"},
            ExpressionAttributeValues=floats_to_decimals(
                {
                    ":pfs": quality_result["final_score"],
                    ":qp": quality_result["quality_passed"],
                    ":qf": quality_result["feedback"],
                    ":qi": quality_result["issues"],
                    ":ps": quality_result["scores"],
                    ":pcs": quality_result["criteria_score"],
                    ":pds": quality_result["dimension_score"],
                    ":now": now,
                    ":status": quality_status,
                    ":job_id": job_id,
                    ":rf": retirement_flag,
                    ":rfd": retirement_age_days,
                    ":rld": (
                        retirement_llm_result.get("retirement_llm_decision", False)
                        if retirement_llm_result
                        else False
                    ),
                    ":rlr": (
                        retirement_llm_result.get("retirement_llm_reason", "")
                        if retirement_llm_result
                        else ""
                    ),
                    ":rlc": (
                        retirement_llm_result.get("retirement_llm_confidence", 0)
                        if retirement_llm_result
                        else 0
                    ),
                }
            ),
        )

        # Enrichment runs for ALL articles (passed and failed)
        # Check if previous run was truncated — use higher max_tokens (doubled, capped at 64000)
        enrich_max_tokens_override = None
        skip_enrichment_too_large = False
        if (
            prev_enrichment_summary
            and "ENRICHMENT_TRUNCATED:max_tokens=" in prev_enrichment_summary
        ):
            try:
                prev_tokens = int(
                    prev_enrichment_summary.split("max_tokens=")[1].split(":")[0]
                )
                doubled = prev_tokens * 2
                if doubled <= 64000:  # Claude Sonnet 4.5 max output tokens
                    enrich_max_tokens_override = doubled
                    log.info(
                        "Using increased max_tokens from previous truncation",
                        article_id=article_id,
                        previous_max_tokens=prev_tokens,
                        new_max_tokens=doubled,
                    )
                else:
                    log.warn(
                        "Article too large to enrich — max tokens exhausted, skipping enrichment",
                        article_id=article_id,
                        previous_max_tokens=prev_tokens,
                        doubled=doubled,
                        max_allowed=64000,
                    )
                    skip_enrichment_too_large = True
            except (ValueError, IndexError):
                pass  # Can't parse — use default

        try:
            if skip_enrichment_too_large:
                # Article exceeded max allowed tokens on previous run — skip enrichment
                enrichment_result = None
                enrichment_applied = False
                table.update_item(
                    Key={"tenant_id": TENANT_ID, "src_kb_article_id": article_id},
                    UpdateExpression="SET #st = :status, enrichment_summary = :es, job_id = :job_id, updated_at = :now",
                    ExpressionAttributeNames={"#st": "pipeline_status"},
                    ExpressionAttributeValues={
                        ":status": "ENRICHMENT_SKIPPED",
                        ":es": f"Article too large to enrich: prev_max_tokens={prev_tokens}, max_allowed=64000",
                        ":job_id": job_id,
                        ":now": now,
                    },
                )
                log.info(
                    "Enrichment skipped — article too large", article_id=article_id
                )
                return {
                    "article_id": article_id,
                    "quality_passed": quality_result["quality_passed"],
                    "enriched": False,
                    "enrich_error": None,
                    "final_status": "ENRICHMENT_SKIPPED",
                }
            else:
                enrichment_result = run_enrichment(
                    article_id,
                    html_content,
                    classification,
                    quality_result["issues"],
                    quality_result["scores"],
                    pre_score=quality_result["final_score"],
                    source_system=source_system,
                    max_tokens_override=enrich_max_tokens_override,
                )
                enriched_bytes, enrichment_applied = apply_enrichment(
                    INGEST_BUCKET,
                    s3_key,
                    article_id,
                    enrichment_result,
                    article_paragraphs,
                    pre_score=quality_result["final_score"],
                )
        except GuardrailBlockedError as gbe:
            log.warn(
                "Article blocked by guardrail during enrichment",
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
                    ":gr": f"Blocked during {gbe.call_type}: {json.dumps(gbe.trace_info, default=str)[:500]}",
                    ":job_id": job_id,
                    ":now": now,
                },
            )
            return {
                "article_id": article_id,
                "quality_passed": quality_result["quality_passed"],
                "enriched": False,
                "enrich_error": None,
                "guardrail_blocked": True,
            }
        except Exception as enrich_err:
            error_msg = str(enrich_err)
            is_timeout = (
                "Read timeout" in error_msg
                or "ConnectTimeout" in error_msg
                or "ReadTimeout" in error_msg
                or "wall-clock limit" in error_msg
            )
            is_truncated = "ENRICHMENT_TRUNCATED" in error_msg
            log.error(
                "Enrichment failed",
                article_id=article_id,
                error=error_msg,
                is_timeout=is_timeout,
                is_truncated=is_truncated,
            )

            if is_timeout or is_truncated:
                # Timeout or truncation — reset to RAW so article is retried on next pipeline run
                # For truncation: store the failed max_tokens so next run uses higher value
                summary = (
                    f"ENRICHMENT_TIMEOUT_RETRY: {error_msg}"
                    if is_timeout
                    else error_msg
                )
                table.update_item(
                    Key={"tenant_id": TENANT_ID, "src_kb_article_id": article_id},
                    UpdateExpression="SET #st = :status, enrichment_summary = :es, job_id = :job_id, updated_at = :now",
                    ExpressionAttributeNames={"#st": "pipeline_status"},
                    ExpressionAttributeValues={
                        ":status": "RAW",
                        ":es": summary,
                        ":job_id": job_id,
                        ":now": now,
                    },
                )
                log.info(
                    "Article reset to RAW for retry on next run",
                    article_id=article_id,
                    reason="timeout" if is_timeout else "truncated",
                )
                return {
                    "article_id": article_id,
                    "quality_passed": False,
                    "enriched": False,
                    "enrich_error": error_msg,
                    "timeout_retry": True,
                }
            else:
                # Non-timeout error — keep existing behavior (mark as GENERATED/QUALITY_FAILED)
                table.update_item(
                    Key={"tenant_id": TENANT_ID, "src_kb_article_id": article_id},
                    UpdateExpression="SET #st = :status, enrichment_summary = :es, "
                    "post_final_score = :pfs, post_scores = :ps, "
                    "post_criteria_score = :pcs, post_dimension_score = :pds, job_id = :job_id, updated_at = :now",
                    ExpressionAttributeNames={"#st": "pipeline_status"},
                    ExpressionAttributeValues=floats_to_decimals(
                        {
                            ":status": (
                                "GENERATED"
                                if quality_result["quality_passed"]
                                else quality_status
                            ),
                            ":es": f"ENRICHMENT_FAILED: {error_msg}",
                            ":now": now,
                            ":pfs": quality_result["final_score"],
                            ":ps": quality_result["scores"],
                            ":pcs": quality_result.get("criteria_score", 0),
                            ":pds": quality_result.get("dimension_score", 0),
                            ":job_id": job_id,
                        }
                    ),
                )
                return {
                    "article_id": article_id,
                    "quality_passed": quality_result["quality_passed"],
                    "enriched": False,
                    "enrich_error": error_msg,
                }

        if enrichment_applied:
            # Score the enriched HTML with original HTML for structural comparison
            enriched_html = enriched_bytes.decode("utf-8")
            enriched_text = extract_plain_text(enriched_html)

            try:
                post_scores = run_post_enrichment_scoring(
                    article_id,
                    enriched_html,
                    classification,
                    original_text=html_content,
                    freshness_score=freshness_score,
                )
            except GuardrailBlockedError as gbe:
                log.warn(
                    "Article blocked by guardrail during post-scoring",
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
                        ":gr": f"Blocked during {gbe.call_type}: {json.dumps(gbe.trace_info, default=str)[:500]}",
                        ":job_id": job_id,
                        ":now": now,
                    },
                )
                return {
                    "article_id": article_id,
                    "quality_passed": quality_result["quality_passed"],
                    "enriched": False,
                    "enrich_error": None,
                    "guardrail_blocked": True,
                }
            except Exception as ps_err:
                log.error(
                    "Post-scoring failed", article_id=article_id, error=str(ps_err)
                )
                dim_config = CFG.get("quality_dimensions", {})
                fallback_dims = {
                    k: 0 for k, v in dim_config.items() if v.get("weight", 0) > 0
                }
                post_scores = {
                    "final_score": 0,
                    "dimension_score": 0,
                    "criteria_score": 0,
                    "scores": fallback_dims,
                    "active_dimensions": list(fallback_dims.keys()),
                }

            pre_score = quality_result["final_score"]
            post_score = post_scores["final_score"]
            _threshold = quality_result["threshold_applied"]
            score_improved = post_score >= pre_score

            if score_improved:
                # Determine final status before uploading
                if quality_result["quality_passed"]:
                    final_status = "GENERATED"
                elif post_score >= _threshold:
                    final_status = "GENERATED"
                    log.info(
                        "Article promoted to GENERATED after enrichment",
                        article_id=article_id,
                        pre_score=pre_score,
                        post_score=post_score,
                        threshold=_threshold,
                    )
                else:
                    # Quality failed and enrichment didn't bring score above threshold
                    log.info(
                        "Quality failed and enrichment insufficient",
                        article_id=article_id,
                        pre_score=pre_score,
                        post_score=post_score,
                        threshold=_threshold,
                    )
                    table.update_item(
                        Key={
                            "tenant_id": TENANT_ID,
                            "src_kb_article_id": article_id,
                        },
                        UpdateExpression="SET #st = :status, enrichment_summary = :es, "
                        "post_final_score = :pfs, post_scores = :ps, "
                        "post_criteria_score = :pcs, post_dimension_score = :pds, job_id = :job_id, updated_at = :now",
                        ExpressionAttributeNames={"#st": "pipeline_status"},
                        ExpressionAttributeValues=floats_to_decimals(
                            {
                                ":status": "QUALITY_FAILED",
                                ":es": "Enrichment attempted but score still below threshold",
                                ":pfs": post_score,
                                ":ps": post_scores["scores"],
                                ":pcs": post_scores.get("criteria_score", 0),
                                ":pds": post_scores.get("dimension_score", 0),
                                ":job_id": job_id,
                                ":now": now,
                            }
                        ),
                    )
                    return {
                        "article_id": article_id,
                        "quality_passed": False,
                        "enriched": False,
                        "enrich_error": None,
                        "final_status": "QUALITY_FAILED",
                    }

                # Upload enriched content for GENERATED articles
                # Get article metadata from DynamoDB for the output payload
                article_record = table.get_item(
                    Key={"tenant_id": TENANT_ID, "src_kb_article_id": article_id}
                ).get("Item", {})
                kb_number = article_record.get("src_kb_article_id", article_id)

                # Store enriched HTML
                generated_file_key = f"{TENANT_ID}/generated/{kb_number}.html"
                log.info(
                    "Uploading enriched HTML to S3",
                    bucket=INGEST_BUCKET,
                    key=generated_file_key,
                    size_bytes=len(enriched_bytes),
                )
                s3.put_object(
                    Bucket=INGEST_BUCKET,
                    Key=generated_file_key,
                    Body=enriched_bytes,
                    ContentType="text/html; charset=utf-8",
                )
                enriched_file_path = f"s3://{INGEST_BUCKET}/{generated_file_key}"

                # Write lean JSON payload for TicketSystem review
                enriched_html = enriched_bytes.decode("utf-8")
                output_payload = {
                    "src_kb_article_id": article_id,
                    "source_system": source_system,
                    "short_description": article_record.get("article_title", ""),
                    "original_text": article_text,
                    "enriched_text": enriched_html,
                    "classification": classification,
                    "classification_confidence": float(
                        article_record.get("classification_confidence", 0)
                    ),
                    "is_duplicate": False,
                    "quality_score_before": float(pre_score),
                    "quality_score_after": float(post_score),
                    "quality_passed": True,
                    "quality_issues": quality_result.get("issues", []),
                    "enrichment_summary": enrichment_result.get(
                        "enrichment_summary", ""
                    ),
                    "retirement_flag": retirement_flag,
                    "retirement_freshness_days": retirement_age_days,
                    "retirement_llm_decision": (
                        retirement_llm_result.get("retirement_llm_decision", False)
                        if retirement_llm_result
                        else False
                    ),
                    "retirement_llm_reason": (
                        retirement_llm_result.get("retirement_llm_reason", "")
                        if retirement_llm_result
                        else ""
                    ),
                    "retirement_llm_confidence": (
                        retirement_llm_result.get("retirement_llm_confidence", 0)
                        if retirement_llm_result
                        else 0
                    ),
                }
                generated_json_key = f"{TENANT_ID}/generated/{kb_number}.json"
                log.info(
                    "Uploading output JSON to S3",
                    bucket=INGEST_BUCKET,
                    key=generated_json_key,
                )
                s3.put_object(
                    Bucket=INGEST_BUCKET,
                    Key=generated_json_key,
                    Body=json.dumps(output_payload, ensure_ascii=False, indent=2),
                    ContentType="application/json",
                )

                table.update_item(
                    Key={"tenant_id": TENANT_ID, "src_kb_article_id": article_id},
                    UpdateExpression="SET #st = :status, enriched_file_path = :efp, enrichment_summary = :es, "
                    "enriched_at = :now, post_final_score = :pfs, post_scores = :ps, "
                    "post_criteria_score = :pcs, post_dimension_score = :pds, job_id = :job_id, updated_at = :now",
                    ExpressionAttributeNames={"#st": "pipeline_status"},
                    ExpressionAttributeValues=floats_to_decimals(
                        {
                            ":status": final_status,
                            ":efp": enriched_file_path,
                            ":es": enrichment_result.get("enrichment_summary", ""),
                            ":now": now,
                            ":pfs": post_score,
                            ":ps": post_scores["scores"],
                            ":pcs": post_scores.get("criteria_score", 0),
                            ":pds": post_scores.get("dimension_score", 0),
                            ":job_id": job_id,
                        }
                    ),
                )
                log.info(
                    "Article enriched and improved",
                    article_id=article_id,
                    enriched_file_path=enriched_file_path,
                    final_status=final_status,
                    pre_score=pre_score,
                    post_score=post_score,
                )
                return {
                    "article_id": article_id,
                    "quality_passed": quality_result["quality_passed"],
                    "enriched": True,
                    "enrich_error": None,
                    "final_status": final_status,
                }
            else:
                # Score dropped — discard enriched version, keep original
                log.info(
                    "Enrichment discarded — score dropped",
                    article_id=article_id,
                    pre_score=pre_score,
                    post_score=post_score,
                )

                table.update_item(
                    Key={"tenant_id": TENANT_ID, "src_kb_article_id": article_id},
                    UpdateExpression="SET #st = :status, enrichment_summary = :es, "
                    "post_final_score = :pfs, post_scores = :ps, "
                    "post_criteria_score = :pcs, post_dimension_score = :pds, job_id = :job_id, updated_at = :now",
                    ExpressionAttributeNames={"#st": "pipeline_status"},
                    ExpressionAttributeValues=floats_to_decimals(
                        {
                            ":status": "ENRICHMENT_SKIPPED",
                            ":es": "Discarded as score dropped",
                            ":pfs": post_score,
                            ":ps": post_scores["scores"],
                            ":pcs": post_scores.get("criteria_score", 0),
                            ":pds": post_scores.get("dimension_score", 0),
                            ":job_id": job_id,
                            ":now": now,
                        }
                    ),
                )
                return {
                    "article_id": article_id,
                    "quality_passed": quality_result["quality_passed"],
                    "enriched": False,
                    "enrich_error": None,
                    "final_status": "ENRICHMENT_SKIPPED",
                }
        else:
            log.info("No enrichment changes needed", article_id=article_id)
            # Even without enrichment changes, check if score meets threshold
            _threshold = quality_result["threshold_applied"]
            if quality_result["quality_passed"]:
                final_status = "GENERATED"
            else:
                final_status = quality_status
            table.update_item(
                Key={"tenant_id": TENANT_ID, "src_kb_article_id": article_id},
                UpdateExpression="SET #st = :status, post_final_score = :pfs, post_scores = :ps, "
                "post_criteria_score = :pcs, post_dimension_score = :pds, job_id = :job_id, updated_at = :now",
                ExpressionAttributeNames={"#st": "pipeline_status"},
                ExpressionAttributeValues=floats_to_decimals(
                    {
                        ":status": final_status,
                        ":pfs": quality_result["final_score"],
                        ":ps": quality_result["scores"],
                        ":pcs": quality_result.get("criteria_score", 0),
                        ":pds": quality_result.get("dimension_score", 0),
                        ":job_id": job_id,
                        ":now": now,
                    }
                ),
            )
            return {
                "article_id": article_id,
                "quality_passed": quality_result["quality_passed"],
                "enriched": False,
                "enrich_error": None,
                "final_status": final_status,
            }

    finally:
        # ── Persist token_usage to DynamoDB ──
        try:
            phase2_tokens = token_tracker.get_article(article_id)
            if phase2_tokens:
                existing = table.get_item(
                    Key={"tenant_id": TENANT_ID, "src_kb_article_id": article_id},
                    ProjectionExpression="token_usage",
                )
                existing_tokens = existing.get("Item", {}).get("token_usage", {})

                merged = {}
                for k, v in existing_tokens.items():
                    merged[k] = int(v) if isinstance(v, (str, float)) else v

                for k, v in phase2_tokens.items():
                    merged[k] = v

                p2_in = (
                    phase2_tokens.get("quality_input", 0)
                    + phase2_tokens.get("enrich_input", 0)
                    + phase2_tokens.get("post_score_input", 0)
                )
                p2_out = (
                    phase2_tokens.get("quality_output", 0)
                    + phase2_tokens.get("enrich_output", 0)
                    + phase2_tokens.get("post_score_output", 0)
                )
                merged["phase2_total_input"] = p2_in
                merged["phase2_total_output"] = p2_out

                p1_in = merged.get("phase1_total_input", 0)
                p1_out = merged.get("phase1_total_output", 0)
                merged["total_input"] = p1_in + p2_in
                merged["total_output"] = p1_out + p2_out

                table.update_item(
                    Key={"tenant_id": TENANT_ID, "src_kb_article_id": article_id},
                    UpdateExpression="SET token_usage = :tu",
                    ExpressionAttributeValues={":tu": floats_to_decimals(merged)},
                )
                token_tracker.log_article_summary(article_id)
                log.info("Token usage persisted to DynamoDB", article_id=article_id)
        except Exception as token_err:
            log.error(
                "Failed to persist token_usage",
                article_id=article_id,
                error=str(token_err),
            )


def prepare_phase2_articles(table, job_id):
    """Called by Step Function before Distributed Map #2.
    Queries EMBEDDED + UNIQUE-pending-quality articles, writes the list to S3
    as a JSON manifest (avoids 256KB Step Function payload limit), and returns
    the S3 location for the Distributed Map ItemReader.
    """
    articles = get_embedded_articles(table)
    pending = get_unique_pending_quality(table)

    seen = {a["src_kb_article_id"] for a in articles}
    for pq in pending:
        if pq["src_kb_article_id"] not in seen:
            articles.append(pq)
            seen.add(pq["src_kb_article_id"])

    result = []
    for a in articles:
        result.append(
            {
                "src_kb_article_id": a["src_kb_article_id"],
                "classification": a.get("classification", ""),
                "source_system": a.get("source_system", "ITSM_KB"),
                "source_file_path": a.get("source_file_path", ""),
                "status": a.get("status", ""),
            }
        )

    log.info("Phase 2 articles prepared", count=len(result))

    # Write manifest to S3
    manifest_key = f"{TENANT_ID}/pipeline-manifests/{job_id}/phase2_articles.json"
    if result:
        s3.put_object(
            Bucket=INGEST_BUCKET,
            Key=manifest_key,
            Body=json.dumps(result),
            ContentType="application/json",
        )
        log.info(
            "Manifest written to S3",
            count=len(result),
            path=f"s3://{INGEST_BUCKET}/{manifest_key}",
        )
    else:
        manifest_key = ""

    return {
        "tenant_id": TENANT_ID,
        "job_id": job_id,
        "manifest_bucket": INGEST_BUCKET,
        "manifest_key": manifest_key,
        "article_count": len(result),
    }


def resolve_phase2_articles(event, table):
    """Resolve articles for Phase 2 processing.

    Distributed Map mode: event contains {"articles": [...]} — each item has
    article_id etc. from the prepare step. We look up full DynamoDB records.

    Legacy mode (no articles in event): fall back to self-query (original behavior).
    """
    batch = event.get("articles")
    if batch:
        articles = []
        for item in batch:
            aid = item.get("src_kb_article_id") or item.get("article_id")
            if not aid:
                continue
            resp = table.get_item(
                Key={"tenant_id": TENANT_ID, "src_kb_article_id": aid}
            )
            record = resp.get("Item")
            if record:
                articles.append(record)
            else:
                log.warn("Article not found in DynamoDB", article_id=aid)
        log.info(
            "Distributed Map articles resolved",
            resolved=len(articles),
            batch_size=len(batch),
        )
        return articles

    # Legacy fallback
    log.info("Legacy mode: querying DynamoDB for EMBEDDED articles")
    return get_embedded_articles(table)


def process_batch(articles, table, now, job_id):
    """Process a batch of articles: dedup (sequential) then quality+enrichment (parallel).
    This is the core logic extracted from the old lambda_handler, minus time guards.
    """
    stats = {
        "phase": "PHASE_2_DEDUP_QUALITY",
        "tenant_id": TENANT_ID,
        "total_checked": len(articles),
        "unique": 0,
        "duplicate": 0,
        "quality_passed": 0,
        "quality_failed": 0,
        "enriched": 0,
        "not_enriched": 0,
        "errors": [],
        "started_at": now,
    }

    # ── Pass 1: Sequential dedup ──────────────────────────────────────────
    marked_as_duplicate = set()
    unique_articles = []

    # Split articles by status — EMBEDDED need dedup, UNIQUE need quality only
    embedded_articles = [a for a in articles if a.get("pipeline_status") == "EMBEDDED"]
    unique_pending = [
        a
        for a in articles
        if a.get("pipeline_status") == "UNIQUE" and a.get("pre_final_score") is None
    ]

    for article in embedded_articles:
        article_id = article["src_kb_article_id"]
        classification = article.get("classification", "")
        source_system = article.get("source_system", "ITSM_KB")
        source_file_path = article.get("source_file_path", "")

        if article_id in marked_as_duplicate:
            log.info("Already marked duplicate in this run", article_id=article_id)
            stats["duplicate"] += 1
            continue

        log.info(
            "Checking article for duplicates",
            article_id=article_id,
            classification=classification,
        )
        try:
            embedding = get_vector(article_id)
            similar = query_similar(article_id, embedding)
            similar = [v for v in similar if v["key"] not in marked_as_duplicate]

            duplicate_of = None
            similarity_score = 0.0
            closest_match_id = None
            closest_match_score = 0.0

            for match in similar:
                distance = match.get("distance")
                if distance is None:
                    continue
                similarity = 1.0 - distance
                matched_id = match["key"]
                log.info(
                    "Similarity match",
                    article_id=article_id,
                    matched_id=matched_id,
                    distance=round(distance, 4),
                    similarity=round(similarity, 4),
                )
                if similarity > closest_match_score:
                    closest_match_score = round(similarity, 4)
                    closest_match_id = matched_id
                if similarity >= (1.0 - COSINE_DISTANCE_THRESHOLD) and not duplicate_of:
                    duplicate_of = matched_id
                    similarity_score = round(similarity, 4)

            if duplicate_of:
                log.info(
                    "Duplicate found",
                    article_id=article_id,
                    duplicate_of=duplicate_of,
                    similarity_score=similarity_score,
                )
                marked_as_duplicate.add(article_id)
                table.update_item(
                    Key={"tenant_id": TENANT_ID, "src_kb_article_id": article_id},
                    UpdateExpression="SET is_duplicate = :t, duplicate_of = :d, similarity_score = :s, "
                    "closest_match_id = :cm, closest_match_score = :cs, "
                    "duplicate_checked_at = :now, #st = :status, job_id = :job_id, updated_at = :now",
                    ExpressionAttributeNames={"#st": "pipeline_status"},
                    ExpressionAttributeValues={
                        ":t": True,
                        ":d": duplicate_of,
                        ":s": str(similarity_score),
                        ":cm": closest_match_id,
                        ":cs": str(closest_match_score),
                        ":now": now,
                        ":status": "DUPLICATE",
                        ":job_id": job_id,
                    },
                )
                update_vector_metadata(
                    article_id, embedding, classification, source_system, "DUPLICATE"
                )
                stats["duplicate"] += 1
            else:
                table.update_item(
                    Key={"tenant_id": TENANT_ID, "src_kb_article_id": article_id},
                    UpdateExpression="SET is_duplicate = :f, closest_match_id = :cm, closest_match_score = :cs, "
                    "duplicate_checked_at = :now, #st = :status, job_id = :job_id, updated_at = :now REMOVE duplicate_of",
                    ExpressionAttributeNames={"#st": "pipeline_status"},
                    ExpressionAttributeValues={
                        ":f": False,
                        ":cm": closest_match_id if closest_match_id else "NONE",
                        ":cs": str(closest_match_score),
                        ":now": now,
                        ":status": "UNIQUE",
                        ":job_id": job_id,
                    },
                )
                update_vector_metadata(
                    article_id, embedding, classification, source_system, "UNIQUE"
                )
                stats["unique"] += 1
                unique_articles.append(
                    {
                        "src_kb_article_id": article_id,
                        "classification": classification,
                        "source_system": source_system,
                        "source_file_path": source_file_path,
                        "embedding": embedding,
                    }
                )
        except Exception as e:
            log.error("Dedup error", article_id=article_id, error=str(e))
            stats["errors"].append({"article_id": article_id, "error": str(e)})

    # Add UNIQUE-pending-quality articles to the quality pass
    for pq in unique_pending:
        if not any(
            u["src_kb_article_id"] == pq["src_kb_article_id"] for u in unique_articles
        ):
            try:
                embedding = get_vector(pq["src_kb_article_id"])
                unique_articles.append(
                    {
                        "src_kb_article_id": pq["src_kb_article_id"],
                        "classification": pq.get("classification", ""),
                        "source_system": pq.get("source_system", "ITSM_KB"),
                        "source_file_path": pq.get("source_file_path", ""),
                        "embedding": embedding,
                    }
                )
            except Exception as e:
                log.error(
                    "Failed to fetch vector for pending article",
                    article_id=pq["src_kb_article_id"],
                    error=str(e),
                )

    # ── Pass 1.5: Freshness resolution ──────────────────────────────────────
    # For each duplicate group, ensure the freshest article is UNIQUE.
    # If a DUPLICATE is fresher than its UNIQUE parent, swap them.
    unique_articles, freshness_swaps = _resolve_freshness(
        table, unique_articles, marked_as_duplicate, now, job_id
    )
    stats["freshness_swaps"] = freshness_swaps
    log.info("Freshness resolution complete", swaps=freshness_swaps)

    # ── Pass 2: Parallel quality + enrichment ─────────────────────────────
    workers = min(MAX_WORKERS, len(unique_articles))
    workers = max(workers, 1)
    log.info(
        "Starting quality and enrichment pass",
        unique_count=len(unique_articles),
        workers=workers,
    )

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {}
        for a in unique_articles:
            futures[
                executor.submit(
                    quality_and_enrich,
                    a["src_kb_article_id"],
                    a["classification"],
                    a["source_system"],
                    a["source_file_path"],
                    a["embedding"],
                    now,
                    job_id,
                )
            ] = a["src_kb_article_id"]

        for future in as_completed(futures):
            article_id = futures[future]
            try:
                result = future.result(
                    timeout=CFG.get("pipeline", {}).get("phase2_article_timeout", 180)
                )
                if result["quality_passed"]:
                    stats["quality_passed"] += 1
                    if result["enriched"]:
                        stats["enriched"] += 1
                    else:
                        stats["not_enriched"] += 1
                else:
                    stats["quality_failed"] += 1
            except TimeoutError:
                log.error("Quality/enrich timeout", article_id=article_id)
                stats["not_enriched"] += 1
                stats["errors"].append(
                    {
                        "article_id": article_id,
                        "error": "QUALITY_ENRICH: future.result() timeout",
                    }
                )
            except Exception as e:
                log.error("Quality/enrich error", article_id=article_id, error=str(e))
                stats["not_enriched"] += 1
                stats["errors"].append(
                    {"article_id": article_id, "error": f"QUALITY_ENRICH: {str(e)}"}
                )

    return stats


def _load_batch_article_ids(manifest_bucket, manifest_key):
    """Load article IDs from the batch manifest in S3.
    Returns a set of article_ids for this batch, or None if manifest not available.
    """
    if not manifest_bucket or not manifest_key:
        log.warn("No manifest provided for aggregate, stats will be cumulative")
        return None
    try:
        resp = s3.get_object(Bucket=manifest_bucket, Key=manifest_key)
        manifest = json.loads(resp["Body"].read().decode("utf-8"))
        ids = {item.get("article_id") for item in manifest if item.get("article_id")}
        log.info(
            "Loaded batch article IDs from manifest", count=len(ids), key=manifest_key
        )
        return ids
    except Exception as e:
        log.warn(
            "Failed to load batch manifest, stats will be cumulative",
            key=manifest_key,
            error=str(e),
        )
        return None


def aggregate_pipeline_stats(
    table, job_id, started_at, change_detection_stats, batch_article_ids=None
):
    """Query articles for this job and compute pipeline stats.
    If batch_article_ids is provided, stats are scoped to that batch only.
    Otherwise falls back to all articles with this job_id (cumulative).
    """
    log.info("Aggregating pipeline stats", job_id=job_id)

    # ── Scan all articles for this tenant ──
    articles = []
    kwargs = {
        "KeyConditionExpression": boto3.dynamodb.conditions.Key("tenant_id").eq(
            TENANT_ID
        )
    }
    while True:
        response = table.query(**kwargs)
        articles.extend(response.get("Items", []))
        if "LastEvaluatedKey" not in response:
            break
        kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    log.info("Total articles fetched for aggregation", count=len(articles))

    # Filter to batch-specific articles if manifest was provided
    if batch_article_ids:
        job_articles = [
            a for a in articles if a.get("src_kb_article_id") in batch_article_ids
        ]
        log.info(
            "Filtered to batch articles",
            batch_count=len(job_articles),
            total=len(articles),
        )
    else:
        job_articles = [a for a in articles if a.get("job_id") == job_id]
    all_articles = articles

    now = datetime.now(timezone.utc).isoformat()

    # ── Status breakdown ──
    status_counts = {}
    for a in job_articles:
        st = a.get("pipeline_status", "UNKNOWN")
        status_counts[st] = status_counts.get(st, 0) + 1

    table_status_counts = {}
    for a in all_articles:
        st = a.get("pipeline_status", "UNKNOWN")
        table_status_counts[st] = table_status_counts.get(st, 0) + 1

    # ── Phase 1: Classification stats ──
    classified = [a for a in job_articles if a.get("classification")]
    cls_breakdown = {}
    confidence_scores = []
    confidence_by_cls = {}
    for a in classified:
        cls = a.get("classification", "UNKNOWN")
        cls_breakdown[cls] = cls_breakdown.get(cls, 0) + 1
        conf = a.get("classification_confidence")
        if conf is not None:
            fconf = float(conf)
            confidence_scores.append(fconf)
            confidence_by_cls.setdefault(cls, []).append(fconf)

    avg_confidence = (
        round(sum(confidence_scores) / len(confidence_scores), 1)
        if confidence_scores
        else 0
    )

    # Per-classification confidence stats (min/q1/median/q3/max for boxplot)
    confidence_by_classification = {}
    for cls, vals in confidence_by_cls.items():
        s = sorted(vals)
        nc = len(s)
        confidence_by_classification[cls] = {
            "min": round(s[0], 1),
            "q1": round(s[max(int(nc * 0.25) - 1, 0)], 1),
            "median": round(s[int(nc * 0.5)], 1),
            "q3": round(s[min(int(nc * 0.75), nc - 1)], 1),
            "max": round(s[-1], 1),
            "count": nc,
        }

    phase1 = {
        "total": len(job_articles),
        "classified": len(classified),
        "embedded": sum(1 for a in job_articles if a.get("embedded_at")),
        "failed": len(job_articles) - len(classified),
        "classification_breakdown": cls_breakdown,
        "avg_confidence": avg_confidence,
        "confidence_by_classification": confidence_by_classification,
    }

    # ── Phase 2: Dedup + Quality stats ──
    duplicates = [a for a in job_articles if a.get("pipeline_status") == "DUPLICATE"]
    unique = [a for a in job_articles if a.get("is_duplicate") is False]
    quality_passed = [
        a for a in job_articles if a.get("pipeline_status") == "GENERATED"
    ]
    quality_failed = [
        a for a in job_articles if a.get("pipeline_status") == "QUALITY_FAILED"
    ]
    enrichment_skipped = [
        a for a in job_articles if a.get("pipeline_status") == "ENRICHMENT_SKIPPED"
    ]
    guardrail_blocked = [
        a for a in job_articles if a.get("pipeline_status") == "GUARDRAIL_BLOCKED"
    ]
    enriched = [a for a in job_articles if a.get("enriched_file_path")]

    total_dedup_checked = len(duplicates) + len(unique)
    total_quality_checked = (
        len(quality_passed) + len(quality_failed) + len(enrichment_skipped)
    )
    pre_quality_passed = sum(1 for a in job_articles if a.get("quality_passed") is True)

    # Duplicates by classification
    duplicates_by_cls = {}
    for a in duplicates:
        cls = a.get("classification", "UNKNOWN")
        duplicates_by_cls[cls] = duplicates_by_cls.get(cls, 0) + 1

    phase2 = {
        "total": total_dedup_checked,
        "unique": len(unique),
        "duplicate": len(duplicates),
        "duplicate_rate": (
            round(100 * len(duplicates) / total_dedup_checked, 1)
            if total_dedup_checked
            else 0
        ),
        "duplicates_by_classification": duplicates_by_cls,
        "quality_passed": len(quality_passed),
        "quality_failed": len(quality_failed),
        "enrichment_skipped": len(enrichment_skipped),
        "guardrail_blocked": len(guardrail_blocked),
        "quality_pass_rate": (
            round(100 * pre_quality_passed / total_quality_checked, 1)
            if total_quality_checked
            else 0
        ),
        "enriched": len(enriched),
        "enrichment_not_applied": total_quality_checked - len(enriched),
        "failed": sum(
            1
            for a in job_articles
            if a.get("pipeline_status") in ("EMBEDDED", "UNIQUE")
            and a.get("pre_final_score") is None
        ),
    }

    # ── Quality summary (all score-related aggregations grouped here) ──
    scored_articles = [a for a in job_articles if a.get("pre_final_score") is not None]
    pre_scores = [float(a["pre_final_score"]) for a in scored_articles]
    post_scores = [
        float(a.get("post_final_score") or a["pre_final_score"])
        for a in scored_articles
    ]

    # Active quality dimensions from config
    dim_config = CFG.get("quality_dimensions", {})
    active_dims = [k for k, v in dim_config.items() if v.get("weight", 0) > 0]

    quality_summary = {}
    if pre_scores:
        pre_sorted = sorted(pre_scores)
        post_sorted = sorted(post_scores)
        n = len(pre_scores)

        def percentile(sorted_list, p):
            idx = int(len(sorted_list) * p / 100)
            idx = min(idx, len(sorted_list) - 1)
            return round(sorted_list[idx], 1)

        deltas = [post_scores[i] - pre_scores[i] for i in range(n)]
        improved = sum(1 for d in deltas if d > 0)
        declined = sum(1 for d in deltas if d < 0)
        no_change = sum(1 for d in deltas if d == 0)

        threshold = float(CFG["quality_thresholds"].get("default", 70))
        above_pre = sum(1 for s in pre_scores if s >= threshold)
        above_post = sum(1 for s in post_scores if s >= threshold)

        # ── Score buckets (for bar chart) ──
        bucket_bins = [(0, 40), (40, 60), (60, 75), (75, 90), (90, 101)]
        bucket_labels = ["0-39", "40-59", "60-74", "75-89", "90-100"]
        pre_buckets = {lbl: 0 for lbl in bucket_labels}
        post_buckets = {lbl: 0 for lbl in bucket_labels}
        for s in pre_scores:
            for (lo, hi), lbl in zip(bucket_bins, bucket_labels):
                if lo <= s < hi:
                    pre_buckets[lbl] += 1
                    break
        for s in post_scores:
            for (lo, hi), lbl in zip(bucket_bins, bucket_labels):
                if lo <= s < hi:
                    post_buckets[lbl] += 1
                    break

        # ── Criteria / dimension score averages ──
        pre_criteria = [
            float(a.get("pre_criteria_score", 0))
            for a in scored_articles
            if a.get("pre_criteria_score") is not None
        ]
        post_criteria = [
            float(a.get("post_criteria_score", 0))
            for a in scored_articles
            if a.get("post_criteria_score") is not None
        ]
        pre_dim_scores = [
            float(a.get("pre_dimension_score", 0))
            for a in scored_articles
            if a.get("pre_dimension_score") is not None
        ]
        post_dim_scores = [
            float(a.get("post_dimension_score", 0))
            for a in scored_articles
            if a.get("post_dimension_score") is not None
        ]

        # ── Per-dimension averages (pre & post) ──
        def _dim_avg(articles_list, prefix):
            """Compute avg per dimension from pre_scores or post_scores map field."""
            field = "pre_scores" if prefix == "pre" else "post_scores"
            totals = {d: 0.0 for d in active_dims}
            counts = {d: 0 for d in active_dims}
            for a in articles_list:
                scores_map = a.get(field)
                if not isinstance(scores_map, dict):
                    continue
                for d in active_dims:
                    val = scores_map.get(d)
                    if val is not None:
                        totals[d] += float(val)
                        counts[d] += 1
            return {
                d: round(totals[d] / counts[d], 1) if counts[d] else 0
                for d in active_dims
            }

        avg_pre_dims = _dim_avg(scored_articles, "pre")
        avg_post_dims = _dim_avg(scored_articles, "post")

        # ── Dimension averages by classification ──
        dim_by_cls = {}
        for a in scored_articles:
            cls = a.get("classification", "UNKNOWN")
            if cls not in dim_by_cls:
                dim_by_cls[cls] = {
                    "pre": {d: [] for d in active_dims},
                    "post": {d: [] for d in active_dims},
                }
            pre_map = a.get("pre_scores")
            post_map = a.get("post_scores")
            if isinstance(pre_map, dict):
                for d in active_dims:
                    v = pre_map.get(d)
                    if v is not None:
                        dim_by_cls[cls]["pre"][d].append(float(v))
            if isinstance(post_map, dict):
                for d in active_dims:
                    v = post_map.get(d)
                    if v is not None:
                        dim_by_cls[cls]["post"][d].append(float(v))

        dimension_by_classification = {}
        for cls, data in dim_by_cls.items():
            dimension_by_classification[cls] = {
                "pre": {
                    d: (
                        round(sum(data["pre"][d]) / len(data["pre"][d]), 1)
                        if data["pre"][d]
                        else 0
                    )
                    for d in active_dims
                },
                "post": {
                    d: (
                        round(sum(data["post"][d]) / len(data["post"][d]), 1)
                        if data["post"][d]
                        else 0
                    )
                    for d in active_dims
                },
            }

        # ── Dimension averages: passed vs failed ──
        passed_articles = [
            a for a in scored_articles if a.get("pipeline_status") == "GENERATED"
        ]
        failed_articles = [
            a for a in scored_articles if a.get("pipeline_status") == "QUALITY_FAILED"
        ]
        dim_avg_passed = _dim_avg(passed_articles, "pre")
        dim_avg_failed = _dim_avg(failed_articles, "pre")

        # ── Threshold promotion stats ──
        originally_passed = sum(
            1 for a in scored_articles if a.get("quality_passed") is True
        )
        originally_failed = sum(
            1 for a in scored_articles if a.get("quality_passed") is False
        )
        promoted = sum(
            1
            for a in scored_articles
            if a.get("quality_passed") is False
            and float(a.get("post_final_score") or 0) >= threshold
        )
        still_below = originally_failed - promoted

        quality_summary = {
            "total_scored": n,
            "threshold": threshold,
            # Final score stats
            "avg_pre_score": round(sum(pre_scores) / n, 1),
            "avg_post_score": round(sum(post_scores) / n, 1),
            "median_pre_score": percentile(pre_sorted, 50),
            "median_post_score": percentile(post_sorted, 50),
            "pre_p25": percentile(pre_sorted, 25),
            "pre_p75": percentile(pre_sorted, 75),
            "post_p25": percentile(post_sorted, 25),
            "post_p75": percentile(post_sorted, 75),
            "min_pre_score": round(pre_sorted[0], 1),
            "max_pre_score": round(pre_sorted[-1], 1),
            "min_post_score": round(post_sorted[0], 1),
            "max_post_score": round(post_sorted[-1], 1),
            "avg_delta": round(sum(deltas) / n, 1),
            "improved": improved,
            "declined": declined,
            "no_change": no_change,
            "above_threshold_pre": above_pre,
            "above_threshold_post": above_post,
            # Score buckets (for distribution bar chart)
            "pre_score_buckets": pre_buckets,
            "post_score_buckets": post_buckets,
            # Criteria / dimension score component averages
            "avg_pre_criteria_score": (
                round(sum(pre_criteria) / len(pre_criteria), 1) if pre_criteria else 0
            ),
            "avg_post_criteria_score": (
                round(sum(post_criteria) / len(post_criteria), 1)
                if post_criteria
                else 0
            ),
            "avg_pre_dimension_score": (
                round(sum(pre_dim_scores) / len(pre_dim_scores), 1)
                if pre_dim_scores
                else 0
            ),
            "avg_post_dimension_score": (
                round(sum(post_dim_scores) / len(post_dim_scores), 1)
                if post_dim_scores
                else 0
            ),
            # Per-dimension averages (pre & post)
            "avg_pre_dimensions": avg_pre_dims,
            "avg_post_dimensions": avg_post_dims,
            # Per-dimension averages by classification
            "dimension_by_classification": dimension_by_classification,
            # Per-dimension averages: passed vs failed
            "dimension_avg_passed": dim_avg_passed,
            "dimension_avg_failed": dim_avg_failed,
            # Threshold promotion breakdown
            "originally_passed": originally_passed,
            "originally_failed": originally_failed,
            "promoted": promoted,
            "still_below": still_below,
        }

    # ── Token usage aggregation ──
    total_tokens = {
        "phase1_input": 0,
        "phase1_output": 0,
        "phase2_input": 0,
        "phase2_output": 0,
        "total_input": 0,
        "total_output": 0,
        "by_call_type": {},
    }
    for a in job_articles:
        tu = a.get("token_usage", {})
        if not tu:
            continue
        total_tokens["phase1_input"] += int(tu.get("phase1_total_input", 0))
        total_tokens["phase1_output"] += int(tu.get("phase1_total_output", 0))
        total_tokens["phase2_input"] += int(tu.get("phase2_total_input", 0))
        total_tokens["phase2_output"] += int(tu.get("phase2_total_output", 0))
        total_tokens["total_input"] += int(tu.get("total_input", 0))
        total_tokens["total_output"] += int(tu.get("total_output", 0))
        for k, v in tu.items():
            if k.endswith("_input") or k.endswith("_output"):
                if k not in (
                    "phase1_total_input",
                    "phase1_total_output",
                    "phase2_total_input",
                    "phase2_total_output",
                    "total_input",
                    "total_output",
                ):
                    total_tokens["by_call_type"][k] = total_tokens["by_call_type"].get(
                        k, 0
                    ) + int(v)

    # ── Efficiency metrics ──
    try:
        from datetime import datetime as dt_parse

        start_dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        end_dt = datetime.now(timezone.utc)
        duration_seconds = round((end_dt - start_dt).total_seconds(), 1)
    except Exception:
        duration_seconds = 0

    total_processed = (
        len(quality_passed)
        + len(quality_failed)
        + len(enrichment_skipped)
        + len(guardrail_blocked)
        + len(duplicates)
    )
    efficiency = {
        "duration_seconds": duration_seconds,
        "articles_per_minute": (
            round(total_processed / (duration_seconds / 60), 1)
            if duration_seconds > 0
            else 0
        ),
        "error_rate": (
            round(100 * phase2["failed"] / len(job_articles), 1) if job_articles else 0
        ),
    }

    # ── Build final result ──
    # ── Language breakdown ──
    language_counts = {}
    for a in job_articles:
        lang = a.get("language", "unknown")
        language_counts[lang] = language_counts.get(lang, 0) + 1

    result = {
        "tenant_id": TENANT_ID,
        "job_id": job_id,
        "articles_in_job": len(job_articles),
        "articles_in_table": len(all_articles),
        "status_breakdown": status_counts,
        "table_snapshot": table_status_counts,
        "change_detection": change_detection_stats,
        "phase1": phase1,
        "phase2": phase2,
        "quality_summary": floats_to_decimals(quality_summary),
        "token_usage": floats_to_decimals(total_tokens),
        "efficiency": floats_to_decimals(efficiency),
        "language_breakdown": language_counts,
        "aggregated_at": now,
    }

    log.info(
        "Pipeline stats aggregated",
        job_id=job_id,
        total_articles=len(job_articles),
        quality_passed=len(quality_passed),
        quality_failed=len(quality_failed),
        enrichment_skipped=len(enrichment_skipped),
        guardrail_blocked=len(guardrail_blocked),
        duplicates=len(duplicates),
    )

    return result


def lambda_handler(event, context):
    """Supports four modes via the event:

    1. {"action": "prepare"} — Called by Step Function before Distributed Map #2.
       Returns list of articles for the Map to iterate over.

    2. {"action": "process", "articles": [...]} — Called by Distributed Map #2.
       Processes a batch of articles (dedup + quality + enrichment).

    3. {"action": "aggregate"} — Called after Phase 2 completes.
       Queries DynamoDB for all articles in this job, computes full pipeline stats.

    4. {} or legacy — Falls back to original self-query behavior for manual testing.
    """
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
    action = event.get("action", "")

    # ── Prepare mode: return article list for Distributed Map ─────────────
    if action == "prepare":
        result = prepare_phase2_articles(table, job_id)
        result["tenant_code"] = event.get("tenant_code", "")
        result["tenant_id"] = event.get("tenant_id", result.get("tenant_id", ""))
        result["home_region"] = event.get("home_region", "")
        return result

    # ── Aggregate mode: compute full pipeline stats from DynamoDB ─────────
    if action == "aggregate":
        batch_article_ids = _load_batch_article_ids(
            event.get("manifest_bucket", ""),
            event.get("manifest_key", ""),
        )
        result = aggregate_pipeline_stats(
            table,
            job_id,
            event.get("started_at", now),
            event.get("change_detection_stats", {}),
            batch_article_ids=batch_article_ids,
        )
        result["tenant_code"] = event.get("tenant_code", "")
        result["tenant_id"] = event.get("tenant_id", result.get("tenant_id", ""))
        result["home_region"] = event.get("home_region", "")
        return result

    # ── Process mode: handle a batch from Distributed Map ─────────────────
    articles = resolve_phase2_articles(event, table)
    log.info("Articles to process", count=len(articles))

    try:
        stats = process_batch(articles, table, now, job_id)
    except Exception as handler_err:
        log.error("Handler error", error=str(handler_err))
        stats = {
            "phase": "PHASE_2_DEDUP_QUALITY",
            "tenant_id": TENANT_ID,
            "total_checked": len(articles),
            "errors": [{"error": f"HANDLER: {str(handler_err)}"}],
        }

    stats["completed_at"] = datetime.now(timezone.utc).isoformat()

    token_summary = token_tracker.log_grand_summary()
    stats["token_usage"] = token_summary

    log.info("Phase 2 complete", stats=stats)

    return {"tenant_id": TENANT_ID, "phase2_dedup_stats": stats}


# deploy 1775215262

# force deploy
