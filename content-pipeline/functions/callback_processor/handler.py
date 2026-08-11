"""
Lambda: Webhook Handler (SQS-based)

Consumes approval/rejection messages from an SQS queue. Another team sends
messages when a Knowledge Manager approves or rejects an article in TicketSystem.

Message format (from external team):
  {
    "approval_id": "00000000000000000000000000000000",
    "ticket_id": "RITM0000000",
    "ticket_system": "TicketSystem",
    "decision": "rejected",
    "approver_id": "jane.doe",
    "approver_name": "Jane Doe",
    "catalog item": "KB Ingestion",
    "comments": "Test again",
    "decided_at": "2026-03-31 08:04:57",
    "rejection_reason": null
  }

Flow:
  1. Parse SQS message body
  2. Use ticket_id (RITM number) to query GSI ritm-number-index
  3. Update article_metadata: pipeline_status → KM_APPROVED or KM_REJECTED
  4. Store approval details (approver_id, approver_name, comments, decided_at)

Environment variables:
  ARTICLE_TABLE  - DynamoDB article_metadata table name
"""

import json
import os
import boto3
import logging
from datetime import datetime, timezone

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

ARTICLE_TABLE = os.environ.get("ARTICLE_TABLE", "")
ENV_CODE = os.environ.get("ENV_CODE")
REGION_CODE = os.environ.get("REGION_CODE")
REGION = os.environ.get("AWS_REGION", "")

# Validate required environment variables
if not ENV_CODE:
    raise ValueError(
        "ENV_CODE environment variable is required for vector bucket name construction"
    )
if not REGION_CODE:
    raise ValueError(
        "REGION_CODE environment variable is required for vector bucket name construction"
    )
if not ARTICLE_TABLE:
    raise ValueError("ARTICLE_TABLE environment variable is required")

dynamodb = boto3.resource("dynamodb", region_name=REGION)
s3vectors = boto3.client("s3vectors", region_name=REGION)
article_table = dynamodb.Table(ARTICLE_TABLE)

DECISION_MAP = {
    "approved": "KM_APPROVED",
    "rejected": "KM_REJECTED",
}


def _find_article_by_ritm(ritm_number):
    """Query GSI ritm-number-index to find the article matching this RITM number."""
    logger.info("Querying ritm-number-index GSI: ritm_number=%s", ritm_number)
    response = article_table.query(
        IndexName="ritm-number-index",
        KeyConditionExpression=boto3.dynamodb.conditions.Key("ritm_number").eq(
            ritm_number
        ),
    )
    items = response.get("Items", [])
    if not items:
        logger.warning("No article found for RITM: ritm_number=%s", ritm_number)
        return None
    logger.info(
        "Article found: tenant=%s, article_id=%s",
        items[0].get("tenant_id"),
        items[0].get("src_kb_article_id"),
    )
    return items[0]


def _delete_vector_from_index(tenant_id, article_id, home_region=None):
    """Delete article vector from S3 Vectors index.

    Args:
        tenant_id: Tenant ID (tenant_code)
        article_id: Article ID (used as vector key)
        home_region: Tenant's home region for resource naming

    Returns:
        bool: True if deletion succeeded or vector didn't exist, False on error
    """
    # Construct vector bucket name: {tenant_id}-{env_code}-{region_code}-{home_region}-doc-vectors
    if home_region:
        vector_bucket = f"{tenant_id}-{ENV_CODE}-{REGION_CODE}-{home_region}-doc-vectors"
        vector_index = f"{tenant_id}-{home_region}-doc-vectors"
    else:
        vector_bucket = f"{tenant_id}-{ENV_CODE}-{REGION_CODE}-doc-vectors"
        vector_index = f"{tenant_id}-doc-vectors"

    try:
        logger.info(
            f"Deleting vector from S3 Vectors: tenant={tenant_id}, article={article_id}, bucket={vector_bucket}, index={vector_index}"
        )
        s3vectors.delete_vectors(
            vectorBucketName=vector_bucket, indexName=vector_index, keys=[article_id]
        )
        logger.info(f"Vector deleted successfully: article={article_id}")
        return True
    except s3vectors.exceptions.ResourceNotFoundException:
        logger.warning(
            f"Vector not found (already deleted or never existed): article={article_id}"
        )
        return True  # Not an error - vector doesn't exist
    except Exception as e:
        logger.error(f"Failed to delete vector for article {article_id}: {e}")
        return False


def _process_message(message):
    """Process a single approval/rejection message.

    Returns dict with processing result for logging.
    """
    # Log full incoming review decision
    logger.info(
        "REVIEW_DECISION: Received — full message: %s",
        json.dumps(message, default=str)[:2000],
    )

    ticket_id = message.get("ticket_id", "")
    decision_raw = (message.get("decision") or "").strip().lower()
    approval_id = message.get("approval_id", "")

    if not ticket_id:
        logger.warning(
            f"SKIP: missing ticket_id in message (approval_id={approval_id})"
        )
        return {"status": "skipped", "reason": "missing ticket_id"}

    if decision_raw not in DECISION_MAP:
        logger.warning(f"SKIP: invalid decision '{decision_raw}' for {ticket_id}")
        return {"status": "skipped", "reason": f"invalid decision: {decision_raw}"}

    # Look up article by RITM number
    article = _find_article_by_ritm(ticket_id)
    if not article:
        logger.warning(f"SKIP: no article found for RITM {ticket_id}")
        return {"status": "skipped", "reason": f"article not found for {ticket_id}"}

    tenant_id = article["tenant_id"]
    article_id = article["src_kb_article_id"]
    home_region = article.get("home_region", "")
    tenant_code = article.get("tenant_code", "")
    current_status = article.get("pipeline_status", "")

    # Guard against duplicate processing
    if current_status in ("KM_APPROVED", "KM_REJECTED"):
        logger.info(f"SKIP: {article_id} already {current_status}, ignoring duplicate")
        return {
            "status": "skipped",
            "reason": f"already {current_status}",
            "article_id": article_id,
        }

    new_status = DECISION_MAP[decision_raw]
    now = datetime.now(timezone.utc).isoformat()

    update_expr = (
        "SET #st = :status,"
        " approval_id = :aid,"
        " approver_id = :approver_id,"
        " approver_name = :approver_name,"
        " approval_comments = :comments,"
        " approval_decided_at = :decided_at,"
        " updated_at = :now"
    )
    expr_values = {
        ":status": new_status,
        ":aid": approval_id,
        ":approver_id": message.get("approver_id", ""),
        ":approver_name": message.get("approver_name", ""),
        ":comments": (message.get("comments") or "").strip(),
        ":decided_at": message.get("decided_at", now),
        ":now": now,
    }

    if decision_raw == "rejected":
        update_expr += ", rejection_reason = :reason"
        expr_values[":reason"] = message.get("rejection_reason") or message.get(
            "comments", ""
        )

    try:
        article_table.update_item(
            Key={"tenant_id": tenant_id, "src_kb_article_id": article_id},
            UpdateExpression=update_expr,
            ExpressionAttributeNames={"#st": "pipeline_status"},
            ExpressionAttributeValues=expr_values,
            ConditionExpression="attribute_exists(src_kb_article_id)",
        )
    except dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
        logger.error(f"ERROR: article {article_id} disappeared during update")
        return {
            "status": "error",
            "reason": "article disappeared",
            "article_id": article_id,
        }
    except Exception as e:
        logger.error(f"ERROR: DynamoDB update failed for {article_id}: {e}")
        raise  # Let SQS retry

    # Delete vector from S3 Vectors if article was rejected
    if decision_raw == "rejected":
        vector_deleted = _delete_vector_from_index(
            tenant_code, article_id, home_region=home_region
        )
        if not vector_deleted:
            logger.warning(
                f"Vector deletion failed for rejected article {article_id}, but continuing"
            )

    logger.info(
        "REVIEW_DECISION: Processed — ticket_id=%s, decision=%s, article_id=%s, tenant=%s, approver=%s, comments=%s",
        ticket_id,
        new_status,
        article_id,
        tenant_id,
        message.get("approver_id", "unknown"),
        (message.get("comments") or "")[:200],
    )
    return {
        "status": "processed",
        "article_id": article_id,
        "ticket_id": ticket_id,
        "decision": new_status,
    }


def lambda_handler(event, context):
    """SQS event handler. Processes each record from the approval queue."""
    processed = 0
    skipped = 0
    errors = 0
    results = []

    for record in event.get("Records", []):
        message_id = record.get("messageId", "unknown")
        logger.info("Processing SQS message: id=%s", message_id)
        try:
            message = json.loads(record.get("body", "{}"))
            logger.info(
                "Message parsed: ticket_id=%s, decision=%s, approver=%s",
                message.get("ticket_id"),
                message.get("decision"),
                message.get("approver_id"),
            )
        except json.JSONDecodeError:
            logger.error(
                "Invalid JSON in SQS message: id=%s, body=%s",
                message_id,
                record.get("body", "")[:200],
            )
            errors += 1
            continue

        try:
            result = _process_message(message)
            results.append(result)

            if result["status"] == "processed":
                processed += 1
            elif result["status"] == "skipped":
                skipped += 1
            else:
                errors += 1
        except Exception as e:
            logger.error(f"ERROR: failed to process message: {e}")
            errors += 1
            raise  # Re-raise so SQS retries this message

    logger.info(f"Summary: processed={processed}, skipped={skipped}, errors={errors}")
    return {"processed": processed, "skipped": skipped, "errors": errors}
