"""
Two-tier AppConfig loader -- shared by all pipeline Lambdas.

Loads config from two AppConfig profiles via the Lambda extension:
  1. SHARED config (models, truncation, quality dimensions, dedup, pipeline)
  2. TENANT config (resource names, prompt ARNs, guardrail, quality thresholds)

Tenant values override shared values via deep merge.

Tenant resolution:
  - If APPCONFIG_APP env var is set (legacy per-tenant Lambda), uses it directly.
  - If not set (shared Lambda), derives the tenant AppConfig app name from
    tenant_code using the convention: {tenant_code}-{env_code}-{region_code}-content_pipeline_config

Config is cached per tenant_code for warm start reuse.

Usage in handlers:
    from config_provider import load_config

    # At module level — safe, returns empty dict until init() is called
    CFG = load_config()

    def lambda_handler(event, context):
        # First thing: init config with tenant_code from event
        tenant_code = event.get('tenant_code', '')
        CFG = load_config(tenant_code=tenant_code)
        ...
"""

import os
import json
import urllib.request
from urllib.parse import quote
import logging

# The AppConfig Lambda extension always serves configuration from this fixed
# local HTTP endpoint. Requests are restricted to it to avoid any possibility
# of urllib opening file:// or other unexpected schemes.
_APPCONFIG_EXTENSION_BASE = "http://localhost:2772"

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

_config_cache = {}  # keyed by tenant_code or '_env'


def _deep_merge(base, override):
    """Recursively merge override into base. Override values win."""
    merged = base.copy()
    for key, val in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(val, dict):
            merged[key] = _deep_merge(merged[key], val)
        else:
            merged[key] = val
    return merged


def _fetch_profile(app, env_name, profile):
    """Fetch a single AppConfig profile from the Lambda extension."""
    # URL-encode each path segment and pin the request to the local AppConfig
    # extension endpoint so a crafted tenant/profile name cannot alter the host
    # or scheme.
    url = (
        f"{_APPCONFIG_EXTENSION_BASE}"
        f"/applications/{quote(app, safe='')}"
        f"/environments/{quote(env_name, safe='')}"
        f"/configurations/{quote(profile, safe='')}"
    )
    if not url.startswith(_APPCONFIG_EXTENSION_BASE + "/"):
        raise ValueError(f"[APPCONFIG] refusing to fetch from unexpected URL: {url}")
    try:
        request = urllib.request.Request(url, method="GET")
        # Scheme/host are fixed to the local http AppConfig extension (validated above).
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
        with urllib.request.urlopen(request, timeout=5) as resp:  # nosec B310 - fixed localhost http endpoint
            data = json.loads(resp.read().decode())
            logger.info(f"[APPCONFIG] loaded {profile} from {app}")
            return data
    except Exception as e:
        raise RuntimeError(f"[APPCONFIG] failed to load from {url}: {e}")


def _resolve_tenant_app_name(tenant_code, home_region=None):
    """Derive tenant AppConfig app name from tenant_code + env vars.
    Convention: {tenant_code}-{env_code}-{region_code}-{home_region}-content_pipeline_config
    If home_region is not provided, falls back to legacy format without it.
    """
    env_code = os.environ.get("ENV_CODE", "s")
    region_code = os.environ.get("REGION_CODE", "euw1")
    if home_region:
        return f"{tenant_code}-{env_code}-{region_code}-{home_region}-content_pipeline_config"
    return f"{tenant_code}-{env_code}-{region_code}-content_pipeline_config"


def load_config(tenant_code=None, home_region=None):
    """Load pipeline config: shared profile first, then tenant overlay.

    Args:
        tenant_code: Optional. If provided, resolves tenant AppConfig dynamically.
                     If not provided, falls back to APPCONFIG_APP env var.
                     If neither is available, returns empty dict (safe for module-level init).
        home_region: Optional. Tenant's home region for resource naming.
                     Used in AppConfig app name: {tenant}-{env}-{region}-{home_region}-content_pipeline_config

    Returns merged config dict. Cached per tenant_code+home_region after first call.
    """
    # Resolve tenant AppConfig app name
    tenant_app = os.environ.get("APPCONFIG_APP", "")

    if not tenant_app and not tenant_code:
        # Shared Lambda, no tenant_code yet (module-level call).
        # Return empty dict — handler will call again with tenant_code.
        return {}

    cache_key = f"{tenant_code or tenant_app}_{home_region or ''}" or "_default"
    if cache_key in _config_cache:
        return _config_cache[cache_key]

    env_name = os.environ.get("APPCONFIG_ENV", "")
    shared_app = os.environ.get("APPCONFIG_SHARED_APP", "")
    shared_profile = os.environ.get("APPCONFIG_SHARED_PROFILE", "")

    if not env_name:
        raise RuntimeError("[APPCONFIG] missing env var APPCONFIG_ENV")
    if not shared_app or not shared_profile:
        raise RuntimeError(
            f"[APPCONFIG] missing shared config env vars -- "
            f"APPCONFIG_SHARED_APP='{shared_app}', "
            f"APPCONFIG_SHARED_PROFILE='{shared_profile}'"
        )

    if not tenant_app:
        tenant_app = _resolve_tenant_app_name(tenant_code, home_region)

    tenant_profile = os.environ.get("APPCONFIG_PROFILE", "tenant-config")

    # Fetch and merge
    shared_cfg = _fetch_profile(shared_app, env_name, shared_profile)
    tenant_cfg = _fetch_profile(tenant_app, env_name, tenant_profile)

    merged = _deep_merge(shared_cfg, tenant_cfg)
    _config_cache[cache_key] = merged

    logger.info(
        f"[APPCONFIG] merged config -- "
        f"tenant_code={tenant_code or 'env'}, home_region={home_region or 'none'}, "
        f"shared keys: {list(shared_cfg.keys())}, "
        f"tenant keys: {list(tenant_cfg.keys())}"
    )
    return merged
