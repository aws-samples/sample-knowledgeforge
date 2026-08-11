"""
Batch Processor Module

Handles batching of changed files and sending to SQS FIFO queue.
"""

import os
import json
import uuid
import boto3
from datetime import datetime, timezone
from logger import get_logger

QUEUE_URL = os.environ.get("QUEUE_URL", "")
REGION = os.environ.get("AWS_REGION", "")

sqs = boto3.client("sqs", region_name=REGION)
log = get_logger(lambda_name="file_enumerator")


def create_tenant_batches(changed_files, batch_size):
    """Group changed files by tenant, then split each tenant's files into batches.
    Returns list of (tenant_id, batch) tuples.
    """
    if not changed_files or batch_size <= 0:
        return []

    # Group by tenant
    by_tenant = {}
    for f in changed_files:
        tid = f.get("tenant_code", "unknown")
        by_tenant.setdefault(tid, []).append(f)

    # Split each tenant's files into batches
    tenant_batches = []
    for tid, files in by_tenant.items():
        for i in range(0, len(files), batch_size):
            tenant_batches.append((tid, files[i : i + batch_size]))

    return tenant_batches


def send_batches_to_sqs(tenant_batches, job_id):
    """Send one SQS FIFO message per tenant batch using SendMessageBatch.
    Uses tenant_id as MessageGroupId so different tenants process in parallel
    while same tenant's batches process sequentially.
    Sends up to 10 messages per batch API call for efficiency.
    Returns the number of successfully sent messages.
    """
    sent_count = 0
    total_batches = len(tenant_batches)

    # Track batch number per tenant for logging
    tenant_counters = {}

    # Build all message entries first
    entries = []
    for idx, (tenant_id, batch) in enumerate(tenant_batches):
        tenant_counters[tenant_id] = tenant_counters.get(tenant_id, 0) + 1
        batch_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{tenant_id}-{tenant_counters[tenant_id]:03d}"
        message_body = json.dumps(
            {
                "job_id": job_id,
                "batch_id": batch_id,
                "tenant_code": (
                    batch[0].get("tenant_code", tenant_id) if batch else tenant_id
                ),
                "tenant_id": (
                    batch[0].get("tenant_id", "") if batch else ""
                ),
                "home_region": batch[0].get("home_region", "") if batch else "",
                "batch_number": idx + 1,
                "total_batches": total_batches,
                "files": batch,
            }
        )
        entries.append(
            {
                "Id": str(idx),
                "MessageBody": message_body,
                "MessageGroupId": tenant_id,
                "MessageDeduplicationId": str(uuid.uuid4()),
                "_batch_id": batch_id,
                "_tenant_id": tenant_id,
                "_file_count": len(batch),
            }
        )

    # Send in chunks of 10 (SQS SendMessageBatch limit)
    for i in range(0, len(entries), 10):
        chunk = entries[i : i + 10]
        sqs_entries = [
            {
                "Id": e["Id"],
                "MessageBody": e["MessageBody"],
                "MessageGroupId": e["MessageGroupId"],
                "MessageDeduplicationId": e["MessageDeduplicationId"],
            }
            for e in chunk
        ]
        try:
            resp = sqs.send_message_batch(QueueUrl=QUEUE_URL, Entries=sqs_entries)
            successful = resp.get("Successful", [])
            failed = resp.get("Failed", [])
            sent_count += len(successful)
            if failed:
                for f in failed:
                    log.error(
                        "SQS batch send failed",
                        message_id=f["Id"],
                        error=f.get("Message", ""),
                        code=f.get("Code", ""),
                    )
        except Exception as e:
            log.error(
                "SQS SendMessageBatch failed",
                chunk_start=i,
                chunk_size=len(chunk),
                error=str(e),
            )
            continue

        for e in chunk:
            log.info(
                "SQS message sent",
                job_id=job_id,
                batch_id=e["_batch_id"],
                tenant_id=e["_tenant_id"],
                file_count=e["_file_count"],
            )

    log.info(
        "All SQS messages sent",
        total_sent=sent_count,
        total_batches=total_batches,
        tenants=list(tenant_counters.keys()),
        batches_per_tenant=dict(tenant_counters),
    )
    return sent_count
