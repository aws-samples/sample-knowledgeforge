"""
Article JSON formatting and S3 upload.
Builds the standardized doc_payload envelope and writes to tenant-segregated S3 paths.
"""
import json
import logging
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def build_article_json(
    article_text: str,
    short_description: str,
    tenant: str,
) -> tuple[str, dict]:
    """
    Build the standardized JSON envelope for a generated article.
    Returns (article_uuid, json_dict) with top-level 'doc_payload' key.
    """
    article_uuid = str(uuid.uuid4())
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return article_uuid, {
        "src_kb_article_id": article_uuid,
        "source_system": "CONTENT_GENERATOR",
        "article_title": short_description,
        "kb_author": "AI GENERATED",
        "workflow_state": "Draft",
        "full_text": article_text,
        "can_read_user_criteria": "",
        "sys_class_name": "knowledge_document",
        "created_ts_utc": now_utc,
        "sys_created_by": "docforge integration",
        "sys_domain": tenant,
        "language": "en",
        "valid_to": "",
        "active": "",
        "description": "",
    }


def write_article_to_s3(
    s3_client, bucket: str, tenant: str, article_type: str,
    article_uuid: str, article_json: dict,
) -> bool:
    """Upload article JSON to s3://{bucket}/{tenant}/{article_type}/{date}/{uuid}.json"""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    key = f"{tenant}/{article_type}/{today}/{article_uuid}.json"
    try:
        s3_client.put_object(
            Bucket=bucket, Key=key,
            Body=json.dumps(article_json, indent=2, default=str),
            ContentType="application/json",
        )
        logger.info("Uploaded %s article to s3://%s/%s", article_type, bucket, key)
        return True
    except Exception as exc:
        logger.error(
            "Failed to upload %s article for tenant '%s' (uuid: %s): %s",
            article_type, tenant, article_uuid, exc,
        )
        return False
