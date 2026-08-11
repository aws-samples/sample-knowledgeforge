"""
Write Manifest Lambda

Invoked by the Step Function per tenant. Takes a tenant's pre-detected changed
files and writes them to S3 as a JSON manifest for the Classify & Embed phase.

S3 paths use tenant_id (UUID) for consistency with dedup/classify_embed:
  {tenant_uuid}/pipeline-manifests/{job_id}/phase1_articles.json
  {tenant_uuid}/pipeline-cache/prompts.json

Input:
  {
    "action": "write_manifest",
    "tenant_code": "orgAlpha",
    "tenant_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    "job_id": "KNOWLEDGE_CURATION_batch-20250101T033000-abc123",
    "files": [
      {"article_id": "orgalpha_abc123", "source_file_path": "s3://...", "reason": "NEW"},
      ...
    ]
  }

Output:
  {
    "tenant_code": "orgAlpha",
    "tenant_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    "job_id": "KNOWLEDGE_CURATION_batch-20250101T033000-abc123",
    "manifest_bucket": "shared-s-euw1-doc-pipeline",
    "manifest_key": "d275b175-.../pipeline-manifests/KNOWLEDGE_CURATION_.../phase1_articles.json",
    "article_count": 5,
    "stats": {"new": 3, "updated": 1, "retry": 1}
  }
"""

import json
import os

import boto3
from botocore.config import Config as BotoConfig

from logger import get_logger
from config_provider import load_config

PIPELINE_BUCKET = os.environ.get('PIPELINE_BUCKET', '')
REGION = os.environ.get('AWS_REGION', '')

s3 = boto3.client('s3', region_name=REGION)
bedrock_agent = boto3.client('bedrock-agent', region_name=REGION,
                             config=BotoConfig(retries={'mode': 'adaptive', 'total_max_attempts': 10}))
log = get_logger(lambda_name='write_manifest')


def _resolve_prompt(key, arn):
    """Fetch system and user text from a managed prompt ARN."""
    try:
        parts = arn.split('/')
        prompt_id = parts[-1].split(':')[0]
        version = parts[-1].split(':')[1] if ':' in parts[-1] else None
        kwargs = {'promptIdentifier': prompt_id}
        if version:
            kwargs['promptVersion'] = version
        detail = bedrock_agent.get_prompt(**kwargs)
        variant = detail['variants'][0]
        chat = variant.get('templateConfiguration', {}).get('chat', {})
        sys_text = ''.join(b.get('text', '') for b in chat.get('system', []) if 'text' in b)
        user_text = ''
        for msg in chat.get('messages', []):
            if msg.get('role') == 'user':
                user_text = ''.join(c.get('text', '') for c in msg.get('content', []))
        return {'system': sys_text, 'user': user_text}
    except Exception as e:
        log.warn('Failed to resolve prompt', key=key, error=str(e))
        return None


def _resolve_and_cache_prompts(tenant_code, tenant_uuid, home_region=None):
    """Load tenant config, resolve all prompts via GetPrompt, save to S3.
    Runs once per batch before the distributed map starts.
    The 40 map children read from S3 instead of calling GetPrompt.

    Args:
        tenant_code: Short code for AppConfig resolution
        tenant_uuid: UUID for S3 path (matches dedup's read path)
        home_region: Tenant's home region for AppConfig app name resolution
    """
    try:
        cfg = load_config(tenant_code=tenant_code, home_region=home_region)
        prompt_arns = cfg.get('prompts', {})
        if not prompt_arns:
            log.info('No prompt ARNs in tenant config, skipping cache', tenant_code=tenant_code)
            return

        prompt_content = {}
        for key, arn in prompt_arns.items():
            resolved = _resolve_prompt(key, arn)
            if resolved:
                prompt_content[key] = resolved

        if prompt_content:
            cache_key = f'{tenant_uuid}/pipeline-cache/prompts.json'
            s3.put_object(
                Bucket=PIPELINE_BUCKET, Key=cache_key,
                Body=json.dumps(prompt_content),
                ContentType='application/json',
            )
            log.info('Prompts cached to S3', tenant_code=tenant_code,
                     tenant_uuid=tenant_uuid, key=cache_key, prompt_count=len(prompt_content))
    except Exception as e:
        log.warn('Prompt caching failed, map children will resolve individually',
                 tenant_code=tenant_code, error=str(e))


def lambda_handler(event, context):
    """Write a tenant's changed files list to S3 as a phase1 manifest.
    Uses tenant_id (UUID) for S3 paths to be consistent with dedup/classify_embed.
    """
    tenant_code = event.get('tenant_code', '')
    tenant_uuid = event.get('tenant_id', '')
    job_id = event.get('job_id', '')
    files = event.get('files', [])

    # Use UUID for S3 paths; fall back to tenant_code for backward compat
    s3_prefix = tenant_uuid or tenant_code

    if not s3_prefix or not job_id:
        log.error('Missing tenant_id/tenant_code or job_id in event',
                  tenant_code=tenant_code, tenant_id=tenant_uuid, job_id=job_id)
        raise ValueError('tenant_id (or tenant_code) and job_id are required')

    if not files:
        log.warn('No files to write manifest for', tenant_code=tenant_code,
                 tenant_id=tenant_uuid, job_id=job_id)

    manifest_key = f'{s3_prefix}/pipeline-manifests/{job_id}/phase1_articles.json'

    # Build manifest entries (same format the Classify & Embed Lambda expects)
    manifest_entries = []
    stats = {}
    for f in files:
        reason = f.get('reason', 'UNKNOWN')
        stats[reason] = stats.get(reason, 0) + 1
        manifest_entries.append({
            'article_id': f['article_id'],
            'source_file_path': f['source_file_path'],
            'reason': reason,
        })

    try:
        s3.put_object(
            Bucket=PIPELINE_BUCKET,
            Key=manifest_key,
            Body=json.dumps(manifest_entries, indent=2),
            ContentType='application/json',
        )
    except Exception as e:
        log.error('Failed to write manifest to S3',
                  tenant_code=tenant_code, tenant_id=tenant_uuid,
                  job_id=job_id, manifest_key=manifest_key, error=str(e))
        raise

    log.info('Manifest written',
             tenant_code=tenant_code,
             tenant_id=tenant_uuid,
             job_id=job_id,
             manifest_key=manifest_key,
             article_count=len(manifest_entries))

    # Pre-resolve prompts and cache to S3 so distributed map children
    # read from S3 instead of calling GetPrompt (avoids throttling at scale)
    _resolve_and_cache_prompts(tenant_code, s3_prefix, home_region=event.get('home_region', ''))

    return {
        'tenant_code': tenant_code,
        'tenant_id': tenant_uuid,
        'home_region': event.get('home_region', ''),
        'job_id': job_id,
        'manifest_bucket': PIPELINE_BUCKET,
        'manifest_key': manifest_key,
        'article_count': len(manifest_entries),
        'stats': stats,
    }
