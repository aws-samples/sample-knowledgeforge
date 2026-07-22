"""
Change Detection Module

Handles Glue catalog queries, change detection logic, and stuck article identification.
"""

import os
import boto3
from logger import get_logger
from utils import tenant_s3_prefix
from tenant_discovery import run_athena_query

GLUE_DATABASE = os.environ.get('GLUE_DATABASE', '')
GLUE_TABLE = os.environ.get('GLUE_TABLE', '')
ATHENA_OUTPUT_LOCATION = os.environ.get('ATHENA_OUTPUT_LOCATION', '')
JOB_STATUS_TABLE = os.environ.get('JOB_STATUS_TABLE', '')
SOURCE_BUCKET = os.environ.get('SOURCE_BUCKET', '')
REGION = os.environ.get('AWS_REGION', '')

athena = boto3.client('athena', region_name=REGION)
dynamodb = boto3.resource('dynamodb', region_name=REGION)
log = get_logger(lambda_name='file_enumerator')


def get_last_pipeline_run(tenant_id):
    """Get the latest completed pipeline run timestamp for a tenant.
    Queries pipeline_job_status for the most recent COMPLETED batch record.
    With composite keys (job_id#batch_id), we paginate until we find one.
    Returns ISO timestamp string or None if no prior run.
    """
    job_table = dynamodb.Table(JOB_STATUS_TABLE)
    try:
        kwargs = {
            'KeyConditionExpression': boto3.dynamodb.conditions.Key('tenant_id').eq(tenant_id),
            'ScanIndexForward': False,
            'Limit': 50,
        }
        while True:
            response = job_table.query(**kwargs)
            for item in response.get('Items', []):
                if 'REVIEW_COUNTER' in item.get('job_id', ''):
                    continue
                if item.get('Status') == 'COMPLETED' and item.get('completed_at'):
                    log.info('Found last pipeline run',
                             tenant_id=tenant_id, completed_at=item['completed_at'],
                             job_id=item['job_id'])
                    return item['completed_at']
            if 'LastEvaluatedKey' not in response:
                break
            kwargs['ExclusiveStartKey'] = response['LastEvaluatedKey']
        log.info('No prior completed run found for tenant', tenant_id=tenant_id)
        return None
    except Exception as e:
        log.error('Failed to query last pipeline run', tenant_id=tenant_id, error=str(e))
        return None


def query_glue_changed_articles_with_metadata(tenant_id, since_timestamp):
    """Query Glue table via Athena for articles with ALL metadata.
    OPTIMIZED: Fetches complete article metadata in one query (no S3 reads needed).
    Full load (since_timestamp=None): returns all articles for the tenant.
    Incremental: returns only articles where last_updated_ts_utc > since_timestamp.
    """
    # Sanitize inputs to prevent SQL injection
    safe_tenant = tenant_id.replace("'", "''")

    # Select metadata fields from Glue table (NO full_text - read from source S3 instead)
    sql = f"""
    SELECT 
        src_kb_article_id,
        article_title,
        created_ts_utc,
        last_updated_ts_utc,
        kb_author,
        language,
        kb_valid_to_ts,
        sys_class_name,
        sys_domain,
        description,
        active,
        status,
        tenant_code,
        aws_region
    FROM "{GLUE_DATABASE}"."{GLUE_TABLE}"
    WHERE src_tenant_id = '{safe_tenant}'
    """
    
    if since_timestamp:
        # Convert ISO 8601 format (2026-05-11T10:44:49.150Z) to Athena TIMESTAMP format (2026-05-11 10:44:49.150)
        safe_ts = since_timestamp.replace("'", "''").replace('T', ' ').rstrip('Z')
        sql += f" AND last_updated_ts_utc > TIMESTAMP '{safe_ts}'"

    try:
        rows = run_athena_query(sql)
        articles = []
        for row in rows:
            if len(row) >= 12 and row[0]:  # Ensure we have all fields (removed full_text)
                article = {
                    'src_kb_article_id': row[0],
                    'article_title': row[1],
                    'created_ts_utc': row[2],
                    'last_updated_ts_utc': row[3],
                    'kb_author': row[4],
                    'language': row[5] or 'en',
                    'kb_valid_to_ts': row[6],
                    'sys_class_name': row[7],
                    'sys_domain': row[8],
                    'description': row[9],
                    'active': row[10],
                    'status': row[11],
                    'tenant_code': row[12] if len(row) > 12 else '',
                    'aws_region': row[13] if len(row) > 13 else '',
                    '_source_s3_bucket': SOURCE_BUCKET,
                    '_source_s3_key': f'{tenant_s3_prefix(tenant_id)}{row[0]}.json',
                }
                articles.append(article)
        
        log.info('Glue/Athena query returned articles with metadata (OPTIMIZED - no full_text, read from source S3)',
                 tenant_id=tenant_id, count=len(articles),
                 mode='full_load' if not since_timestamp else 'incremental')
        return articles
    except Exception as e:
        log.error('Athena query failed for changed articles',
                  tenant_id=tenant_id, error=str(e))
        return []


def build_multi_tenant_athena_query(tenant_timestamps):
    """Build a single Athena query for all tenants with their respective timestamps.
    OPTIMIZED: One query for all tenants instead of N separate queries.
    
    Args:
        tenant_timestamps: dict of {tenant_id: last_run_timestamp or None}
        
    Returns:
        SQL query string
    """
    conditions = []
    incremental_tenants = []
    full_load_tenants = []
    
    for tenant_id, last_run in tenant_timestamps.items():
        safe_tenant = tenant_id.replace("'", "''")
        
        if last_run:
            # Incremental: filter by timestamp
            # Convert ISO 8601 format (2026-05-11T10:44:49.150Z) to Athena TIMESTAMP format (2026-05-11 10:44:49.150)
            safe_ts = last_run.replace("'", "''").replace('T', ' ').rstrip('Z')
            conditions.append(
                f"(src_tenant_id = '{safe_tenant}' AND last_updated_ts_utc > TIMESTAMP '{safe_ts}')"
            )
            incremental_tenants.append(f"{tenant_id}(since:{last_run[:10]})")
        else:
            # Full load: no timestamp filter
            conditions.append(f"(src_tenant_id = '{safe_tenant}')")
            full_load_tenants.append(tenant_id)
    
    where_clause = " OR ".join(conditions)
    
    sql = f"""
    SELECT 
        src_tenant_id,
        src_kb_article_id,
        article_title,
        created_ts_utc,
        last_updated_ts_utc,
        kb_author,
        language,
        kb_valid_to_ts,
        sys_class_name,
        sys_domain,
        description,
        active,
        status,
        tenant_code,
        aws_region
    FROM "{GLUE_DATABASE}"."{GLUE_TABLE}"
    WHERE {where_clause}
    ORDER BY src_tenant_id
    """
    
    log.info('Built multi-tenant Athena query',
             tenant_count=len(tenant_timestamps),
             incremental_count=len(incremental_tenants),
             full_load_count=len(full_load_tenants),
             incremental_tenants=incremental_tenants[:5],  # Log first 5
             full_load_tenants=full_load_tenants[:5],  # Log first 5
             query_length=len(sql))
    
    return sql


def start_multi_tenant_athena_query(tenant_timestamps):
    """Start a single Athena query for all tenants and return the query_id.
    OPTIMIZED: One query for all tenants instead of N separate queries.
    
    Args:
        tenant_timestamps: dict of {tenant_id: last_run_timestamp or None}
        
    Returns:
        query_id string or None on failure
    """
    sql = build_multi_tenant_athena_query(tenant_timestamps)
    
    try:
        execution = athena.start_query_execution(
            QueryString=sql,
            QueryExecutionContext={'Database': GLUE_DATABASE},
            ResultConfiguration={'OutputLocation': ATHENA_OUTPUT_LOCATION},
        )
        query_id = execution['QueryExecutionId']
        log.info('Multi-tenant Athena query started',
                 query_id=query_id,
                 tenant_count=len(tenant_timestamps))
        return query_id
    except Exception as e:
        log.error('Failed to start multi-tenant Athena query',
                  error=str(e),
                  tenant_count=len(tenant_timestamps))
        return None


def wait_and_fetch_multi_tenant_results(query_id, max_wait_seconds=300):
    """Wait for multi-tenant Athena query to complete and return results grouped by tenant.
    
    Args:
        query_id: Athena query execution ID
        max_wait_seconds: Maximum time to wait (default 300s for large queries)
        
    Returns:
        dict of {tenant_id: [article_dicts]}
    """
    import time as _time
    elapsed = 0
    poll_interval = 2
    poll_count = 0
    
    log.info('Waiting for multi-tenant Athena query to complete',
             query_id=query_id, max_wait_seconds=max_wait_seconds)
    
    # Poll for completion
    while elapsed < max_wait_seconds:
        status_resp = athena.get_query_execution(QueryExecutionId=query_id)
        state = status_resp['QueryExecution']['Status']['State']
        poll_count += 1
        
        if state == 'SUCCEEDED':
            log.info('Multi-tenant Athena query succeeded',
                     query_id=query_id, elapsed_seconds=round(elapsed, 1), poll_count=poll_count)
            break
        if state in ('FAILED', 'CANCELLED'):
            reason = status_resp['QueryExecution']['Status'].get('StateChangeReason', 'Unknown')
            log.error('Multi-tenant Athena query failed',
                     query_id=query_id, state=state, reason=reason, elapsed_seconds=round(elapsed, 1))
            return {}
        
        # Log progress every 30 seconds
        if poll_count % 15 == 0:
            log.info('Still waiting for Athena query',
                     query_id=query_id, state=state, elapsed_seconds=round(elapsed, 1))
        
        _time.sleep(poll_interval)
        elapsed += poll_interval
        poll_interval = min(poll_interval * 1.5, 10)

    if elapsed >= max_wait_seconds:
        athena.stop_query_execution(QueryExecutionId=query_id)
        log.error('Multi-tenant Athena query timed out',
                 query_id=query_id, elapsed_seconds=elapsed, max_wait_seconds=max_wait_seconds)
        return {}

    # Fetch results (paginated)
    log.info('Fetching Athena query results', query_id=query_id)
    rows = []
    paginator_token = None
    page_count = 0
    
    while True:
        kwargs = {'QueryExecutionId': query_id}
        if paginator_token:
            kwargs['NextToken'] = paginator_token
        result = athena.get_query_results(**kwargs)
        page_count += 1
        
        for row in result['ResultSet']['Rows']:
            values = [col.get('VarCharValue', '') for col in row['Data']]
            rows.append(values)
        
        paginator_token = result.get('NextToken')
        if not paginator_token:
            break
        
        # Log progress for large result sets
        if page_count % 10 == 0:
            log.info('Fetching Athena results - pagination in progress',
                     query_id=query_id, pages_fetched=page_count, rows_so_far=len(rows))

    # Skip header row
    if len(rows) > 1:
        rows = rows[1:]
    elif len(rows) == 1:
        rows = []
    
    log.info('Multi-tenant Athena query results fetched',
             query_id=query_id, total_rows=len(rows), pages_fetched=page_count)
    
    # Group articles by tenant_id
    articles_by_tenant = {}
    for row in rows:
        if len(row) >= 13 and row[0] and row[1]:  # Ensure we have tenant_id and article_id
            tenant_id = row[0]
            article = {
                'src_kb_article_id': row[1],
                'article_title': row[2],
                'created_ts_utc': row[3],
                'last_updated_ts_utc': row[4],
                'kb_author': row[5],
                'language': row[6] or 'en',
                'kb_valid_to_ts': row[7],
                'sys_class_name': row[8],
                'sys_domain': row[9],
                'description': row[10],
                'active': row[11],
                'status': row[12],
                'tenant_code': row[13] if len(row) > 13 else '',
                'aws_region': row[14] if len(row) > 14 else '',
                '_source_s3_bucket': SOURCE_BUCKET,
                '_source_s3_key': f'{tenant_s3_prefix(tenant_id)}{row[1]}.json',
            }
            articles_by_tenant.setdefault(tenant_id, []).append(article)
    
    log.info('Articles grouped by tenant',
             tenant_count=len(articles_by_tenant),
             total_articles=len(rows),
             articles_per_tenant={tid: len(arts) for tid, arts in articles_by_tenant.items()})
    
    return articles_by_tenant


def get_stuck_article_ids(table, tenant_id):
    """Query pipeline-status-index GSI for articles stuck in intermediate states.
    These are articles that failed mid-pipeline and need to be retried.
    Includes ERROR_S3_FILE_MISSING articles for retry (rare case where S3 file was missing).
    Returns a list of article IDs with their source_file_path.
    """
    stuck_statuses = ['RAW', 'CLASSIFIED', 'EMBEDDED', 'ERROR_S3_FILE_MISSING']
    stuck_articles = []

    for status in stuck_statuses:
        kwargs = {
            'IndexName': 'pipeline-status-index',
            'KeyConditionExpression': (
                boto3.dynamodb.conditions.Key('tenant_id').eq(tenant_id)
                & boto3.dynamodb.conditions.Key('pipeline_status').eq(status)
            ),
            'ProjectionExpression': 'src_kb_article_id, source_file_path',
        }
        while True:
            response = table.query(**kwargs)
            for item in response.get('Items', []):
                stuck_articles.append({
                    'article_id': item['src_kb_article_id'],
                    'source_file_path': item.get('source_file_path', ''),
                    'reason': 'RETRY_STUCK',
                })
            if 'LastEvaluatedKey' not in response:
                break
            kwargs['ExclusiveStartKey'] = response['LastEvaluatedKey']

    if stuck_articles:
        log.info('Found stuck articles for retry',
                 tenant_id=tenant_id, count=len(stuck_articles),
                 statuses=stuck_statuses)
    return stuck_articles
