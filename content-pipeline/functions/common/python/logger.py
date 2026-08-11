"""
Structured JSON logger for all pipeline Lambdas.

Emits one JSON object per log line so CloudWatch Logs Insights can query
fields like job_id, article_id, tenant_id, level, etc. directly.

Usage:
    from logger import get_logger
    log = get_logger(tenant_id='example', job_id='run-123')
    log.info('Processing started', article_id='art-1', extra_field='value')
    log.error('Something failed', article_id='art-1', error=str(e))
"""
import json
import sys
import time
from datetime import datetime, timezone


class StructuredLogger:
    """JSON logger that includes correlation fields in every log line."""

    def __init__(self, tenant_id='', job_id='', lambda_name=''):
        self._base = {
            'tenant_id': tenant_id,
            'job_id': job_id,
            'lambda': lambda_name,
        }

    def set_job_id(self, job_id):
        self._base['job_id'] = job_id

    def set_context(self, **kwargs):
        """Add persistent fields to all future log lines."""
        self._base.update(kwargs)

    def _emit(self, level, message, **kwargs):
        entry = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'level': level,
            'message': message,
            **self._base,
            **kwargs,
        }
        # Remove empty string values to keep logs clean
        entry = {k: v for k, v in entry.items() if v != ''}
        print(json.dumps(entry, default=str), flush=True)

    def info(self, message, **kwargs):
        self._emit('INFO', message, **kwargs)

    def warn(self, message, **kwargs):
        self._emit('WARN', message, **kwargs)

    def error(self, message, **kwargs):
        self._emit('ERROR', message, **kwargs)

    def debug(self, message, **kwargs):
        self._emit('DEBUG', message, **kwargs)


def get_logger(tenant_id='', job_id='', lambda_name=''):
    """Create a structured logger instance."""
    return StructuredLogger(tenant_id=tenant_id, job_id=job_id, lambda_name=lambda_name)
