"""
Thread-safe token usage tracker shared by all pipeline Lambdas.

Tracks input/output tokens per call type and per article, with helpers
to log summaries and persist to DynamoDB.

Usage:
    from usage_tracker import TokenTracker
    tracker = TokenTracker(logger=log)
    tracker.record('classify', 'art-1', input_tokens=500, output_tokens=120)
    tracker.log_article_summary('art-1')
    totals = tracker.log_grand_summary()
"""
import json
import threading


class TokenTracker:
    """Thread-safe token usage tracker."""

    def __init__(self, logger=None):
        self._lock = threading.Lock()
        self._totals = {}
        self._per_article = {}
        self._log = logger

    def record(self, call_type, article_id, input_tokens, output_tokens,
               cache_read_tokens=0, cache_write_tokens=0):
        with self._lock:
            for suffix, val in [('input', input_tokens), ('output', output_tokens),
                                ('cache_read', cache_read_tokens), ('cache_write', cache_write_tokens)]:
                key = f'{call_type}_{suffix}'
                self._totals[key] = self._totals.get(key, 0) + val
                if article_id:
                    art = self._per_article.setdefault(article_id, {})
                    art[key] = art.get(key, 0) + val
        if self._log:
            self._log.info('Token usage recorded', call_type=call_type,
                           article_id=article_id, input_tokens=input_tokens,
                           output_tokens=output_tokens,
                           cache_read_tokens=cache_read_tokens,
                           cache_write_tokens=cache_write_tokens)

    def get_totals(self):
        with self._lock:
            return dict(self._totals)

    def get_article(self, article_id):
        with self._lock:
            return dict(self._per_article.get(article_id, {}))

    def log_article_summary(self, article_id):
        data = self.get_article(article_id)
        if data:
            total_in = sum(v for k, v in data.items() if k.endswith('_input'))
            total_out = sum(v for k, v in data.items() if k.endswith('_output'))
            total_cache_read = sum(v for k, v in data.items() if k.endswith('_cache_read'))
            total_cache_write = sum(v for k, v in data.items() if k.endswith('_cache_write'))
            if self._log:
                self._log.info('Article token summary', article_id=article_id,
                               total_input=total_in, total_output=total_out,
                               total_cache_read=total_cache_read,
                               total_cache_write=total_cache_write, detail=data)

    def log_grand_summary(self):
        totals = self.get_totals()
        total_in = sum(v for k, v in totals.items() if k.endswith('_input'))
        total_out = sum(v for k, v in totals.items() if k.endswith('_output'))
        total_cache_read = sum(v for k, v in totals.items() if k.endswith('_cache_read'))
        total_cache_write = sum(v for k, v in totals.items() if k.endswith('_cache_write'))
        articles_tracked = len(self._per_article)
        if self._log:
            self._log.info('Grand token summary', articles_tracked=articles_tracked,
                           total_input=total_in, total_output=total_out,
                           total_cache_read=total_cache_read,
                           total_cache_write=total_cache_write, breakdown=totals)
        return {'total_input_tokens': total_in, 'total_output_tokens': total_out,
                'total_cache_read_tokens': total_cache_read,
                'total_cache_write_tokens': total_cache_write,
                'articles_tracked': articles_tracked, 'breakdown': totals}
