"""
Tenant Connector Config Client

Fetches ServiceNow connector config (catalog_item_id, caller_email) from the
tenant-connectors API using IAM SigV4 authentication.

Lambda execution role credentials are used automatically — no manual keys needed.

Usage:
    from tenant_connector import get_connector_config

    config = get_connector_config(tenant_id="d275b175-...", connector_code="SNOW")
    # Returns: {"caller_email": "...", "catalog_item_id": "..."}
"""

import json
import logging
import urllib3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
import botocore.session

logger = logging.getLogger()

# Cache: {tenant_id_connector_code: config_dict}
_connector_cache = {}

http = urllib3.PoolManager()


def get_connector_config(tenant_id, api_url, api_host, region="eu-west-1", connector_code="SNOW"):
    """Fetch tenant connector config from the tenant-connectors API.

    Uses IAM SigV4 signing — Lambda role credentials are picked up automatically.

    Args:
        tenant_id: Tenant UUID (e.g. aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa)
        api_url: Tenant connector API base URL (from env var TENANT_CONNECTOR_API_URL)
        api_host: API Gateway host for SigV4 signing (from env var TENANT_CONNECTOR_API_HOST)
        region: AWS region for signing (default: eu-west-1)
        connector_code: Connector code (default: SNOW)

    Returns:
        Dict with keys: caller_email, catalog_item_id, tenant_id
        Returns None on failure.
    """
    if not api_url or not tenant_id:
        logger.warning("tenant_connector: missing api_url or tenant_id")
        return None

    cache_key = f"{tenant_id}_{connector_code}"
    if cache_key in _connector_cache:
        return _connector_cache[cache_key]

    url = f"{api_url}?tenant_id={tenant_id}&connector_code={connector_code}"

    try:
        # Sign request with Lambda role credentials (automatic)
        session = botocore.session.get_session()
        credentials = session.get_credentials().get_frozen_credentials()

        headers = {"host": api_host}
        aws_request = AWSRequest(method="GET", url=url, headers=headers)
        SigV4Auth(credentials, "execute-api", region).add_auth(aws_request)

        signed_headers = dict(aws_request.headers)
        resp = http.request("GET", url, headers=signed_headers, timeout=10.0)

        if resp.status != 200:
            logger.error(
                "tenant_connector: API returned %d — %s",
                resp.status,
                resp.data.decode()[:500],
            )
            return None

        body = json.loads(resp.data.decode())
        data_list = body.get("data", [])

        if not data_list:
            logger.warning(
                "tenant_connector: no connector config for tenant=%s connector=%s",
                tenant_id,
                connector_code,
            )
            return None

        connector = data_list[0]
        config_json = connector.get("config_json", {})

        result = {
            "caller_email": config_json.get("caller_email", ""),
            "catalog_item_id": config_json.get("catalog_item_id", ""),
            "tenant_id": connector.get("tenant_id", tenant_id),
        }

        _connector_cache[cache_key] = result
        logger.info(
            "tenant_connector: loaded config — tenant=%s, caller_email=%s, catalog_item_id=%s",
            tenant_id,
            result["caller_email"],
            result["catalog_item_id"],
        )
        return result

    except Exception as e:
        logger.error("tenant_connector: failed — %s", str(e))
        return None
