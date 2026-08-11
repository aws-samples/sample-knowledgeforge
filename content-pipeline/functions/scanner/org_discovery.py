"""
Tenant Discovery Module

Discovers tenants from Glue catalog using Athena queries.
"""

import os
import re
import boto3
from logger import get_logger

GLUE_DATABASE = os.environ.get('GLUE_DATABASE', '')
GLUE_TABLE = os.environ.get('GLUE_TABLE', '')
ATHENA_OUTPUT_LOCATION = os.environ.get('ATHENA_OUTPUT_LOCATION', '')
REGION = os.environ.get('AWS_REGION', '')

athena = boto3.client('athena', region_name=REGION)
log = get_logger(lambda_name='file_enumerator')

# Glue/Athena identifiers (database, table) come from deploy-time env vars and
# cannot be bound as query parameters. Validate them against a strict allowlist
# so they can be safely referenced in the FROM clause. All *values* in queries
# are bound via Athena ExecutionParameters instead of string interpolation.
_IDENTIFIER_RE = re.compile(r'^[A-Za-z0-9_]+$')


def validate_identifier(value, name):
    """Ensure a SQL identifier (database/table name) is a safe bare identifier."""
    if not value or not _IDENTIFIER_RE.match(value):
        raise ValueError(f'Invalid SQL identifier for {name}: {value!r}')
    return value


def run_athena_query(sql, max_wait_seconds=120, execution_parameters=None):
    """Execute an Athena query and return result rows (list of lists).
    Polls until query completes or times out.

    execution_parameters: optional list of positional values bound to '?'
    placeholders in the query (Athena parameterized query), preventing SQL
    injection from user-influenced values.
    """
    log.info('Running Athena query', sql=sql[:200])

    query_kwargs = {
        'QueryString': sql,
        'QueryExecutionContext': {'Database': GLUE_DATABASE},
        'ResultConfiguration': {'OutputLocation': ATHENA_OUTPUT_LOCATION},
    }
    if execution_parameters:
        query_kwargs['ExecutionParameters'] = execution_parameters

    try:
        execution = athena.start_query_execution(**query_kwargs)
    except Exception as e:
        log.error('Failed to start Athena query', error=str(e), sql=sql[:200])
        raise

    query_id = execution['QueryExecutionId']
    log.info('Athena query started', query_id=query_id)

    # Poll for completion
    import time as _time
    elapsed = 0
    poll_interval = 2
    while elapsed < max_wait_seconds:
        status_resp = athena.get_query_execution(QueryExecutionId=query_id)
        state = status_resp['QueryExecution']['Status']['State']

        if state == 'SUCCEEDED':
            break
        if state in ('FAILED', 'CANCELLED'):
            reason = status_resp['QueryExecution']['Status'].get('StateChangeReason', 'Unknown')
            log.error('Athena query failed', query_id=query_id, state=state, reason=reason)
            raise RuntimeError(f'Athena query {state}: {reason}')

        _time.sleep(poll_interval)
        elapsed += poll_interval
        poll_interval = min(poll_interval * 1.5, 10)

    if elapsed >= max_wait_seconds:
        log.error('Athena query timed out', query_id=query_id, elapsed=elapsed)
        athena.stop_query_execution(QueryExecutionId=query_id)
        raise RuntimeError(f'Athena query timed out after {max_wait_seconds}s')

    # Fetch results (paginated)
    rows = []
    paginator_token = None
    while True:
        kwargs = {'QueryExecutionId': query_id}
        if paginator_token:
            kwargs['NextToken'] = paginator_token
        result = athena.get_query_results(**kwargs)

        for row in result['ResultSet']['Rows']:
            values = [col.get('VarCharValue', '') for col in row['Data']]
            rows.append(values)

        paginator_token = result.get('NextToken')
        if not paginator_token:
            break

    # Skip header row (first row from Athena is always column names)
    if len(rows) > 1:
        rows = rows[1:]
    elif len(rows) == 1:
        # Only header, no data
        rows = []

    log.info('Athena query complete', query_id=query_id, rows_returned=len(rows))
    return rows


def discover_tenants_from_glue():
    """Query Glue table via Athena to discover all unique tenant IDs with their
    tenant_code and aws_region columns.
    Returns a list of dicts: [{"tenant_id": UUID, "tenant_code": str, "aws_region": str}]
    """
    validate_identifier(GLUE_DATABASE, 'GLUE_DATABASE')
    validate_identifier(GLUE_TABLE, 'GLUE_TABLE')

    # No user-supplied values in this query; the only interpolated tokens are the
    # database/table identifiers, which are validated above and cannot be bound
    # as Athena parameters.
    sql = f"""
    SELECT DISTINCT src_tenant_id, tenant_code, aws_region
    FROM "{GLUE_DATABASE}"."{GLUE_TABLE}"
    WHERE src_tenant_id IS NOT NULL
    ORDER BY src_tenant_id
    """  # nosec B608 - identifiers validated; no value interpolation

    try:
        rows = run_athena_query(sql, max_wait_seconds=60)
        tenants = []
        for row in rows:
            if row and row[0]:
                tenants.append({
                    "tenant_id": row[0],
                    "tenant_code": row[1] if len(row) > 1 else "",
                    "aws_region": row[2] if len(row) > 2 else "",
                })
        
        log.info('Discovered tenants from Glue table',
                 count=len(tenants),
                 tenants=tenants,
                 database=GLUE_DATABASE,
                 table=GLUE_TABLE)
        return tenants
    except Exception as e:
        log.error('Failed to discover tenants from Glue table',
                  error=str(e),
                  database=GLUE_DATABASE,
                  table=GLUE_TABLE)
        # Fallback to empty list - will be handled by caller
        return []


def discover_tenants():
    """Discover tenants from Glue table using Athena query.
    Returns a list of dicts with tenant_id (UUID), tenant_code, and aws_region
    directly from the Glue catalog. No static mapping needed.
    """
    return discover_tenants_from_glue()
