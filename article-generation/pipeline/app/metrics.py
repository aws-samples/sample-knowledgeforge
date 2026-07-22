"""
DynamoDB metrics writer for the ECS article pipeline.

Writes run-level and document-level records to the Metrics Table.
All functions catch exceptions and log errors without re-raising,
so DynamoDB failures never crash the pipeline.
"""

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Records expire after 90 days
TTL_DAYS = 90


def _ttl_epoch() -> int:
    """Return a Unix epoch timestamp 90 days from now."""
    return int(datetime.now(timezone.utc).timestamp()) + (TTL_DAYS * 86400)


def record_run_start(table, run_id: str, tenant: str, start_time: str) -> None:
    """Write an initial run record with status RUNNING.

    Parameters
    ----------
    table : boto3 DynamoDB Table resource
    run_id : str
        UUID identifying this pipeline run.
    tenant : str
        Tenant identifier being processed.
    start_time : str
        ISO 8601 UTC timestamp for the run start.
    """
    try:
        table.put_item(Item={
            "run_id": run_id,
            "record_type": "RUN",
            "status": "RUNNING",
            "start_time": start_time,
            "tenant": tenant,
            "ttl": _ttl_epoch(),
        })
    except Exception:
        logger.exception("Failed to write run-start record for run_id=%s", run_id)


def record_run_complete(table, run_id: str, result: dict, end_time: str) -> None:
    """Update the run record with COMPLETED status and totals.

    Parameters
    ----------
    table : boto3 DynamoDB Table resource
    run_id : str
        UUID identifying this pipeline run.
    result : dict
        Must contain keys: tenants_processed, total_kb_articles,
        total_rca_articles, and token_usage (a dict with input_tokens,
        output_tokens, total_tokens).
    end_time : str
        ISO 8601 UTC timestamp for the run end.
    """
    try:
        table.update_item(
            Key={"run_id": run_id, "record_type": "RUN"},
            UpdateExpression=(
                "SET #s = :status, end_time = :end_time, "
                "tenants_processed = :tp, total_kb_articles = :kb, "
                "total_rca_articles = :rca, token_usage = :tu"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":status": "COMPLETED",
                ":end_time": end_time,
                ":tp": result.get("tenants_processed", 0),
                ":kb": result.get("total_kb_articles", 0),
                ":rca": result.get("total_rca_articles", 0),
                ":tu": result.get("token_usage", {}),
            },
        )
    except Exception:
        logger.exception("Failed to write run-complete record for run_id=%s", run_id)


def record_run_failed(table, run_id: str, error, end_time: str) -> None:
    """Update the run record with FAILED status and error message.

    Parameters
    ----------
    table : boto3 DynamoDB Table resource
    run_id : str
        UUID identifying this pipeline run.
    error : Exception or str
        The error that caused the failure.
    end_time : str
        ISO 8601 UTC timestamp for the run end.
    """
    try:
        table.update_item(
            Key={"run_id": run_id, "record_type": "RUN"},
            UpdateExpression=(
                "SET #s = :status, end_time = :end_time, "
                "error_message = :err"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":status": "FAILED",
                ":end_time": end_time,
                ":err": str(error),
            },
        )
    except Exception:
        logger.exception("Failed to write run-failed record for run_id=%s", run_id)


def record_document(
    table,
    run_id: str,
    tenant: str,
    theme_name: str,
    article_type: str,
    article_uuid: str,
    s3_key: str,
    short_description: str,
) -> None:
    """Write a document-level record for a generated article.

    Parameters
    ----------
    table : boto3 DynamoDB Table resource
    run_id : str
        UUID identifying the pipeline run.
    tenant : str
        Tenant identifier.
    theme_name : str
        Name of the theme that produced this article.
    article_type : str
        ``"KB"`` or ``"RCA"``.
    article_uuid : str
        UUID of the generated article.
    s3_key : str
        S3 object key where the article was written.
    short_description : str
        Short description of the article content.
    """
    try:
        table.put_item(Item={
            "run_id": run_id,
            "record_type": f"DOC#{article_uuid}",
            "tenant": tenant,
            "theme_name": theme_name,
            "article_type": article_type,
            "article_uuid": article_uuid,
            "s3_key": s3_key,
            "short_description": short_description,
            "generation_timestamp": datetime.now(timezone.utc).isoformat(),
            "ttl": _ttl_epoch(),
        })
    except Exception:
        logger.exception(
            "Failed to write document record for run_id=%s article=%s",
            run_id,
            article_uuid,
        )
