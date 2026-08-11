"""
Container entrypoint for the ECS article pipeline.

Polls an SQS queue for S3 event notifications, validates messages,
extracts the tenant from the object key, and runs the pipeline
orchestrator for each valid message.  Handles SIGTERM for graceful
shutdown during ECS deployments, scale-down, or spot reclamation.
"""

import asyncio
import json
import logging
import signal
import sys
from datetime import datetime, timezone
from uuid import uuid4

import boto3

from app.config import load_config, get_clients
from app.metrics import record_run_start, record_run_complete, record_run_failed
from app.pipeline import process_tenant

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

# ── Graceful shutdown ────────────────────────────────────────────────

shutdown_flag = False


def handle_sigterm(signum, frame):
    """Set the shutdown flag so the poll loop exits after the current message."""
    global shutdown_flag
    logger.info("SIGTERM received, initiating graceful shutdown")
    shutdown_flag = True


# ── SQS message helpers ─────────────────────────────────────────────

def validate_message(body_str: str):
    """Parse and validate an SQS message body.

    Returns the tenant string extracted from the S3 object key,
    or ``None`` if the message is invalid.
    """
    try:
        body = json.loads(body_str)
    except (json.JSONDecodeError, TypeError):
        logger.error("SQS message body is not valid JSON")
        return None

    records = body.get("Records")
    if not records or not isinstance(records, list):
        logger.error("SQS message missing 'Records' list")
        return None

    s3_info = records[0].get("s3")
    if not s3_info:
        logger.error("SQS record missing 's3' key")
        return None

    key = s3_info.get("object", {}).get("key", "")
    if not key.endswith("themes.json"):
        logger.error("S3 object key does not end with 'themes.json': %s", key)
        return None

    parts = key.split("/")
    if len(parts) < 2 or not parts[0]:
        logger.error("Cannot extract tenant from S3 object key: %s", key)
        return None

    return parts[0]


# ── Main loop ────────────────────────────────────────────────────────

def main():
    config = load_config("config.yaml")
    signal.signal(signal.SIGTERM, handle_sigterm)

    sqs = boto3.client("sqs", region_name=config.aws.region)
    clients = get_clients(config)
    queue_url = config.sqs.queue_url

    logger.info("Pipeline started — polling %s", queue_url)

    while not shutdown_flag:
        try:
            resp = sqs.receive_message(
                QueueUrl=queue_url,
                MaxNumberOfMessages=config.sqs.max_messages,
                WaitTimeSeconds=config.sqs.wait_time_seconds,
            )
        except Exception:
            logger.exception("SQS receive_message failed, will retry")
            continue

        messages = resp.get("Messages", [])
        for msg in messages:
            # Validate the message
            tenant = validate_message(msg.get("Body", ""))
            if tenant is None:
                logger.warning("Deleting invalid SQS message %s", msg["MessageId"])
                sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=msg["ReceiptHandle"])
                continue

            run_id = str(uuid4())
            start_time = datetime.now(timezone.utc).isoformat()
            record_run_start(clients["dynamodb"], run_id, tenant, start_time)

            try:
                result = asyncio.run(process_tenant(tenant, config, clients, run_id))
                end_time = datetime.now(timezone.utc).isoformat()
                record_run_complete(clients["dynamodb"], run_id, result, end_time)
                sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=msg["ReceiptHandle"])
                logger.info("Run %s completed for tenant '%s'", run_id, tenant)
            except Exception as exc:
                end_time = datetime.now(timezone.utc).isoformat()
                record_run_failed(clients["dynamodb"], run_id, exc, end_time)
                logger.exception("Run %s failed for tenant '%s'", run_id, tenant)

    logger.info("Graceful shutdown complete")
    sys.exit(0)


if __name__ == "__main__":
    main()
