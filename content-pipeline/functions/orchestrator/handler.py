"""
Dispatcher Lambda

Triggered by SQS event source mapping (batch size = 1) on the Batch FIFO Queue.
Starts a single Step Function execution per message and waits synchronously
for the execution to complete before returning.

Always ACKs the message (returns successfully). On failure:
- Resets article status to RAW in DynamoDB
- Sends failed batch to DLQ manually
- Increments consecutive failure counter
- If consecutive failures >= threshold, halts remaining batches

Environment variables:
  STATE_MACHINE_ARN - ARN of the Article Curation Pipeline Step Function
  DLQ_URL - URL of the dead-letter queue for failed batches
  ARTICLE_TABLE - DynamoDB article metadata table name
  MAX_CONSECUTIVE_FAILURES - Circuit breaker threshold (default: 3)
"""

import json
import os
import time
import uuid
from datetime import datetime, timezone

import boto3

from logger import get_logger

STATE_MACHINE_ARN = os.environ.get("STATE_MACHINE_ARN")
DLQ_URL = os.environ.get("DLQ_URL")
ARTICLE_TABLE = os.environ.get("ARTICLE_TABLE")
MAX_CONSECUTIVE_FAILURES = int(os.environ.get("MAX_CONSECUTIVE_FAILURES", "3"))
REGION = os.environ.get("AWS_REGION", "")

# Validate required environment variables
if not STATE_MACHINE_ARN:
    raise ValueError("STATE_MACHINE_ARN environment variable is required")
if not DLQ_URL:
    raise ValueError("DLQ_URL environment variable is required")
if not ARTICLE_TABLE:
    raise ValueError("ARTICLE_TABLE environment variable is required")

sfn = boto3.client("stepfunctions", region_name=REGION)
sqs = boto3.client("sqs", region_name=REGION)
dynamodb = boto3.resource("dynamodb", region_name=REGION)
log = get_logger(lambda_name="dispatcher")

TERMINAL_STATES = {"SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"}
INITIAL_BACKOFF_SECONDS = 5
MAX_BACKOFF_SECONDS = 30

# Track consecutive failures per job in module-level cache (warm Lambda reuse)
_failure_counts = {}


def _generate_execution_name(batch_id):
    """Generate a unique execution name: batch-{batch_id}-{short_uuid}."""
    short_id = uuid.uuid4().hex[:8]
    return f"batch-{batch_id}-{short_id}"[:80]


def _start_execution(files, job_id, batch_id, tenant_code, tenant_id, home_region):
    """Start a Step Function execution with the batch payload."""
    execution_name = _generate_execution_name(batch_id)
    sfn_input = json.dumps(
        {
            "files": files,
            "job_id": job_id,
            "batch_id": batch_id,
            "tenant_code": tenant_code,
            "tenant_id": tenant_id,
            "home_region": home_region,
        }
    )

    log.info(
        "Starting Step Function execution",
        execution_name=execution_name,
        file_count=len(files),
        job_id=job_id,
        batch_id=batch_id,
        sfn_input_preview=sfn_input[:500],
    )

    response = sfn.start_execution(
        stateMachineArn=STATE_MACHINE_ARN,
        name=execution_name,
        input=sfn_input,
    )
    return response["executionArn"]


def _poll_execution(execution_arn):
    """Poll DescribeExecution with exponential backoff until terminal state.
    Gives up after 13 minutes to leave headroom before Lambda timeout (15 min).
    """
    backoff = INITIAL_BACKOFF_SECONDS
    max_wait = 13 * 60  # 13 minutes
    elapsed = 0
    while elapsed < max_wait:
        response = sfn.describe_execution(executionArn=execution_arn)
        status = response["status"]
        if status in TERMINAL_STATES:
            log.info(
                "Execution reached terminal state",
                execution_arn=execution_arn,
                status=status,
            )
            return response
        log.info(
            "Execution running, waiting",
            execution_arn=execution_arn,
            backoff_seconds=backoff,
            elapsed_seconds=round(elapsed),
        )
        time.sleep(backoff)
        elapsed += backoff
        backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)

    log.error(
        "Polling timeout — execution still running after max wait",
        execution_arn=execution_arn,
        elapsed_seconds=round(elapsed),
    )
    return {
        "status": "TIMED_OUT",
        "error": "Dispatcher polling timeout",
        "cause": f"Execution still running after {round(elapsed)}s",
    }


def _reset_articles_to_raw(files):
    """Reset DynamoDB status to RAW for all articles in a failed batch."""
    if not ARTICLE_TABLE:
        log.warn("ARTICLE_TABLE not set, cannot reset articles")
        return
    table = dynamodb.Table(ARTICLE_TABLE)
    now = datetime.now(timezone.utc).isoformat()
    log.info("Resetting articles to RAW", article_count=len(files))
    for f in files:
        try:
            log.info(
                "Resetting article",
                tenant_code=f.get("tenant_id", f.get("tenant_id", "")),
                article_id=f.get("article_id"),
            )
            table.update_item(
                Key={
                    "tenant_id": f.get("tenant_id", f.get("tenant_id", "")),
                    "src_kb_article_id": f["article_id"],
                },
                UpdateExpression="SET #st = :raw, updated_at = :now",
                ExpressionAttributeNames={"#st": "pipeline_status"},
                ExpressionAttributeValues={":raw": "RAW", ":now": now},
            )
        except Exception as e:
            log.error(
                "Failed to reset article", article_id=f.get("article_id"), error=str(e)
            )


def _send_to_dlq(batch_message, error_info):
    """Send failed batch to DLQ for investigation."""
    if not DLQ_URL:
        log.warn("DLQ_URL not set, cannot send to DLQ")
        return
    try:
        sqs.send_message(
            QueueUrl=DLQ_URL,
            MessageBody=json.dumps(
                {
                    "original_batch": batch_message,
                    "error": error_info,
                    "failed_at": datetime.now(timezone.utc).isoformat(),
                }
            ),
            MessageGroupId="failed-batches",
            MessageDeduplicationId=f"dlq-{batch_message.get('batch_id', 'unknown')}-{uuid.uuid4().hex[:8]}",
        )
        log.info("Failed batch sent to DLQ", batch_id=batch_message.get("batch_id"))
    except Exception as e:
        log.error("Failed to send to DLQ", error=str(e))


def _get_failure_count(job_id):
    return _failure_counts.get(job_id, 0)


def _increment_failure_count(job_id):
    _failure_counts[job_id] = _failure_counts.get(job_id, 0) + 1
    return _failure_counts[job_id]


def _reset_failure_count(job_id):
    _failure_counts[job_id] = 0


def lambda_handler(event, context):
    """Entry point invoked by SQS event source mapping (batch size = 1).

    Always returns successfully (ACKs the message). Handles failures internally.
    """
    record = event["Records"][0]
    body = json.loads(record["body"])

    files = body.get("files", [])
    job_id = body.get("job_id", "unknown")
    batch_id = body.get("batch_id", "unknown")
    tenant_code = body.get("tenant_code", "unknown")
    tenant_id = body.get("tenant_id", "")
    home_region = body.get("home_region", "")
    batch_number = body.get("batch_number", 0)
    total_batches = body.get("total_batches", 0)

    log.info(
        "Dispatcher invoked",
        job_id=job_id,
        batch_id=batch_id,
        tenant_code=tenant_code,
        home_region=home_region,
        batch_number=batch_number,
        total_batches=total_batches,
        file_count=len(files),
    )

    # Circuit breaker — reset counter for new job_id (handles warm Lambda reuse)
    if job_id not in _failure_counts:
        _failure_counts.clear()  # New job, clear stale counters from previous runs
        _failure_counts[job_id] = 0

    failure_count = _get_failure_count(job_id)
    if failure_count >= MAX_CONSECUTIVE_FAILURES:
        log.error(
            "Circuit breaker triggered — halting remaining batches",
            job_id=job_id,
            consecutive_failures=failure_count,
            batch_id=batch_id,
        )
        _reset_articles_to_raw(files)
        _send_to_dlq(body, f"Circuit breaker: {failure_count} consecutive failures")
        return {"batch_id": batch_id, "status": "HALTED"}

    try:
        execution_arn = _start_execution(
            files, job_id, batch_id, tenant_code, tenant_id, home_region
        )
        result = _poll_execution(execution_arn)
        status = result["status"]

        if status == "SUCCEEDED":
            _reset_failure_count(job_id)
            log.info(
                "Batch completed successfully",
                job_id=job_id,
                batch_id=batch_id,
                execution_arn=execution_arn,
            )
            return {
                "batch_id": batch_id,
                "status": "SUCCEEDED",
                "execution_arn": execution_arn,
            }
        else:
            error_info = f'{status}: {result.get("error", "Unknown")} - {result.get("cause", "")}'
            count = _increment_failure_count(job_id)
            log.error(
                "Batch failed",
                job_id=job_id,
                batch_id=batch_id,
                status=status,
                consecutive_failures=count,
                execution_arn=execution_arn,
            )
            _reset_articles_to_raw(files)
            _send_to_dlq(body, error_info)
            return {
                "batch_id": batch_id,
                "status": "FAILED",
                "execution_arn": execution_arn,
            }

    except Exception as e:
        count = _increment_failure_count(job_id)
        log.error(
            "Dispatcher error",
            job_id=job_id,
            batch_id=batch_id,
            error=str(e),
            consecutive_failures=count,
        )
        _reset_articles_to_raw(files)
        _send_to_dlq(body, str(e))
        return {"batch_id": batch_id, "status": "ERROR"}
