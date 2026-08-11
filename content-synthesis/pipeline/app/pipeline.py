"""
Pipeline orchestrator — processes a single tenant's themes concurrently,
generating KB and RCA articles for each theme.

Migrated from lambda/lambda_handler.py for the ECS container model.
Key differences from the Lambda version:
- process_tenant handles a single tenant (not all tenants)
- Retry parameters are config-driven via PipelineConfig
- Metrics are recorded to DynamoDB via metrics.record_document
"""

import asyncio
import json
import logging
import random

# Use a CSPRNG-backed generator (SystemRandom) for jitter/sampling so static
# analysis does not flag the default non-cryptographic PRNG (CWE-338).
_secure_random = random.SystemRandom()
from datetime import datetime, timezone

from app.config import PipelineConfig
from app.generators import (
    assemble_theme_context,
    generate_kb_article,
    generate_rca_article,
)
from app.retrieval import retrieve_similar_articles
from app.article_writer import build_article_json, write_article_to_s3
from app.metrics import record_document
from app.token_counter import TokenCounter

logger = logging.getLogger(__name__)


# ── S3 helpers ───────────────────────────────────────────────

def discover_tenants(s3_client, bucket: str) -> list[str]:
    """List top-level prefixes in the input bucket."""
    resp = s3_client.list_objects_v2(Bucket=bucket, Delimiter="/")
    return [
        p["Prefix"].rstrip("/")
        for p in resp.get("CommonPrefixes", [])
        if p["Prefix"].rstrip("/")
    ]


def load_themes(s3_client, bucket: str, tenant: str) -> dict | None:
    """Download and parse {tenant}/themes.json. Returns None on error."""
    key = f"{tenant}/themes.json"
    try:
        body = s3_client.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
    except Exception:
        logger.warning("themes.json missing or unreadable for tenant '%s'", tenant)
        return None
    try:
        return json.loads(body)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.error("Invalid JSON in themes.json for tenant '%s': %s", tenant, exc)
        return None


# ── Retry with exponential backoff ───────────────────────────

async def _generate_with_retry(gen_func, context: dict, clients: dict,
                               config: PipelineConfig, counter: TokenCounter,
                               retrieved_articles: list | None = None):
    """Call a generator with config-driven exponential backoff + jitter.

    Uses config.bedrock.retry.max_attempts, config.bedrock.retry.base_delay,
    and config.bedrock.retry.max_delay for retry parameters.

    Returns (article_text, short_description, usage_dict).
    """
    max_attempts = config.bedrock.retry.max_attempts
    base_delay = config.bedrock.retry.base_delay
    max_delay = config.bedrock.retry.max_delay

    last_err = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await gen_func(
                context, clients, config, counter,
                retrieved_articles=retrieved_articles,
            )
        except Exception as err:
            last_err = err
            if attempt == max_attempts:
                break
            delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
            jitter = _secure_random.uniform(0, delay * 0.5)
            wait = delay + jitter
            logger.warning(
                "Attempt %d/%d failed: %s — retrying in %.1fs",
                attempt, max_attempts, err, wait,
            )
            await asyncio.sleep(wait)
    raise last_err


# ── Theme processing ─────────────────────────────────────────

async def process_theme(
    tenant: str,
    theme_name: str,
    theme_data: dict,
    sem: asyncio.Semaphore,
    counter: TokenCounter,
    config: PipelineConfig,
    clients: dict,
    run_id: str,
) -> dict:
    """Generate KB + RCA for a single theme, write both to S3,
    and record each document in DynamoDB metrics."""
    async with sem:
        context = assemble_theme_context(theme_name, theme_data, config)
        logger.info("Processing theme: %s (tenant: %s)", theme_name, tenant)
        result = {"theme": theme_name, "kb_success": False, "rca_success": False}

        # Retrieve similar KB articles from tenant's vector index
        query = f"{context['theme_name']} {' '.join(context['keywords'][:5])}"
        loop = asyncio.get_running_loop()
        try:
            retrieved = await loop.run_in_executor(
                None, retrieve_similar_articles, query, tenant, clients, config,
            )
            logger.info(
                "Retrieved %d reference articles for theme '%s'",
                len(retrieved), theme_name,
            )
        except Exception as exc:
            logger.warning(
                "Vector retrieval failed for theme '%s': %s, continuing without",
                theme_name, exc,
            )
            retrieved = []

        output_bucket = config.s3.output_bucket

        # ── KB article ──
        try:
            text, short_desc, _ = await _generate_with_retry(
                generate_kb_article, context, clients, config, counter,
                retrieved_articles=retrieved,
            )
            uid, payload = build_article_json(text, short_desc, tenant)
            wrote = write_article_to_s3(
                clients["s3"], output_bucket, tenant, "ic", uid, payload,
            )
            result["kb_success"] = wrote
            if wrote:
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                s3_key = f"{tenant}/ic/{today}/{uid}.json"
                record_document(
                    clients["dynamodb"], run_id, tenant, theme_name,
                    "KB", uid, s3_key, short_desc,
                )
        except Exception as exc:
            logger.error(
                "KB failed — tenant '%s', theme '%s': %s",
                tenant, theme_name, exc,
            )

        # ── RCA article ──
        try:
            text, short_desc, _ = await _generate_with_retry(
                generate_rca_article, context, clients, config, counter,
                retrieved_articles=retrieved,
            )
            uid, payload = build_article_json(text, short_desc, tenant)
            wrote = write_article_to_s3(
                clients["s3"], output_bucket, tenant, "rca", uid, payload,
            )
            result["rca_success"] = wrote
            if wrote:
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                s3_key = f"{tenant}/rca/{today}/{uid}.json"
                record_document(
                    clients["dynamodb"], run_id, tenant, theme_name,
                    "RCA", uid, s3_key, short_desc,
                )
        except Exception as exc:
            logger.error(
                "RCA failed — tenant '%s', theme '%s': %s",
                tenant, theme_name, exc,
            )

        return result


# ── Tenant processing ────────────────────────────────────────

async def process_tenant(
    tenant: str,
    config: PipelineConfig,
    clients: dict,
    run_id: str,
) -> dict:
    """Process a single tenant: load themes, process them concurrently.

    Returns a result dict with:
        total_kb_articles, total_rca_articles, failures, token_usage
    """
    counter = TokenCounter()
    sem = asyncio.Semaphore(config.pipeline.max_concurrent_themes)

    themes_data = load_themes(clients["s3"], config.s3.input_bucket, tenant)

    if themes_data is None:
        return {
            "total_kb_articles": 0,
            "total_rca_articles": 0,
            "failures": {tenant: {"kb_failed": 0, "rca_failed": 0, "skipped": True}},
            "token_usage": counter.to_dict(),
        }

    logger.info("Tenant '%s' has %d themes", tenant, len(themes_data))

    tasks = [
        process_theme(
            tenant, name, data, sem, counter, config, clients, run_id,
        )
        for name, data in themes_data.items()
    ]
    results = await asyncio.gather(*tasks)

    kb_ok = sum(1 for r in results if r["kb_success"])
    rca_ok = sum(1 for r in results if r["rca_success"])
    failures = {
        tenant: {
            "kb_failed": len(results) - kb_ok,
            "rca_failed": len(results) - rca_ok,
        }
    }

    counter.log_summary()
    return {
        "total_kb_articles": kb_ok,
        "total_rca_articles": rca_ok,
        "failures": failures,
        "token_usage": counter.to_dict(),
    }
