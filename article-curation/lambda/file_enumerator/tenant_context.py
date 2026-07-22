"""
Tenant Context Module

Manages per-tenant configuration loading from AppConfig.
Thread-safe container for parallel tenant processing.
"""

from appconfig_loader import load_config
from logger import get_logger

log = get_logger(lambda_name="file_enumerator")


class TenantContext:
    """Thread-safe container for per-tenant config. Passed as parameter instead
    of module globals so multiple tenants can process in parallel.
    """

    __slots__ = (
        "tenant_id",
        "table_name",
        "source_bucket",
        "pipeline_bucket",
        "vector_bucket",
        "vector_index",
        "protected_statuses",
        "cfg",
    )

    def __init__(self, cfg):
        self.tenant_id = cfg["resources"]["tenant_id"]
        self.table_name = cfg["resources"]["table_name"]
        self.source_bucket = cfg["resources"]["source_bucket"]
        self.pipeline_bucket = cfg["resources"]["pipeline_bucket"]
        self.vector_bucket = cfg["resources"]["vector_bucket"]
        self.vector_index = cfg["resources"]["vector_index"]
        self.protected_statuses = set(
            cfg.get("change_detection", {}).get("protected_statuses", [])
        )
        self.cfg = cfg


def load_tenant_context(tenant_id, home_region=None):
    """Load tenant config from AppConfig and return a TenantContext."""
    log.info("Loading tenant config", tenant_id=tenant_id, home_region=home_region)
    cfg = load_config(tenant_code=tenant_id, home_region=home_region)
    ctx = TenantContext(cfg)
    log.info("Tenant config loaded", tenant_id=tenant_id, table=ctx.table_name)
    return ctx
