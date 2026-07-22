"""
Unit tests for pipeline/app/pipeline.py.

Tests cover:
- discover_tenants: S3 prefix listing
- load_themes: JSON parsing, error handling
- _generate_with_retry: config-driven retry, exponential backoff
- process_tenant: end-to-end orchestration with mocked dependencies
"""

import asyncio
import json
import unittest
from unittest.mock import MagicMock, AsyncMock, patch, call

from app.config import PipelineConfig, BedrockRetryConfig
from app.pipeline import (
    discover_tenants,
    load_themes,
    _generate_with_retry,
    process_tenant,
    process_theme,
)
from app.token_counter import TokenCounter


class TestDiscoverTenants(unittest.TestCase):
    def test_returns_prefixes(self):
        s3 = MagicMock()
        s3.list_objects_v2.return_value = {
            "CommonPrefixes": [
                {"Prefix": "tenanta/"},
                {"Prefix": "tenantb/"},
            ]
        }
        result = discover_tenants(s3, "my-bucket")
        self.assertEqual(result, ["tenanta", "tenantb"])
        s3.list_objects_v2.assert_called_once_with(Bucket="my-bucket", Delimiter="/")

    def test_empty_bucket(self):
        s3 = MagicMock()
        s3.list_objects_v2.return_value = {}
        result = discover_tenants(s3, "my-bucket")
        self.assertEqual(result, [])

    def test_filters_empty_prefixes(self):
        s3 = MagicMock()
        s3.list_objects_v2.return_value = {
            "CommonPrefixes": [
                {"Prefix": "tenanta/"},
                {"Prefix": "/"},
            ]
        }
        result = discover_tenants(s3, "my-bucket")
        self.assertEqual(result, ["tenanta"])


class TestLoadThemes(unittest.TestCase):
    def test_valid_json(self):
        themes = {"theme1": {"keywords": ["a"]}}
        body_mock = MagicMock()
        body_mock.read.return_value = json.dumps(themes).encode("utf-8")
        s3 = MagicMock()
        s3.get_object.return_value = {"Body": body_mock}

        result = load_themes(s3, "bucket", "tenanta")
        self.assertEqual(result, themes)

    def test_missing_file(self):
        s3 = MagicMock()
        s3.get_object.side_effect = Exception("NoSuchKey")
        result = load_themes(s3, "bucket", "tenanta")
        self.assertIsNone(result)

    def test_invalid_json(self):
        body_mock = MagicMock()
        body_mock.read.return_value = b"not json"
        s3 = MagicMock()
        s3.get_object.return_value = {"Body": body_mock}

        result = load_themes(s3, "bucket", "tenanta")
        self.assertIsNone(result)


class TestGenerateWithRetry(unittest.TestCase):
    def _make_config(self, max_attempts=3, base_delay=0.01, max_delay=0.1):
        cfg = PipelineConfig()
        cfg.bedrock.retry = BedrockRetryConfig(
            max_attempts=max_attempts,
            base_delay=base_delay,
            max_delay=max_delay,
        )
        return cfg

    def test_succeeds_first_attempt(self):
        gen = AsyncMock(return_value=("text", "desc", {"input_tokens": 1}))
        cfg = self._make_config()
        counter = TokenCounter()

        result = asyncio.run(
            _generate_with_retry(gen, {}, {}, cfg, counter)
        )
        self.assertEqual(result, ("text", "desc", {"input_tokens": 1}))
        gen.assert_called_once()

    def test_succeeds_on_second_attempt(self):
        gen = AsyncMock(side_effect=[
            Exception("throttled"),
            ("text", "desc", {}),
        ])
        cfg = self._make_config(max_attempts=3, base_delay=0.001)
        counter = TokenCounter()

        result = asyncio.run(
            _generate_with_retry(gen, {}, {}, cfg, counter)
        )
        self.assertEqual(result[0], "text")
        self.assertEqual(gen.call_count, 2)

    def test_exhausts_all_attempts(self):
        gen = AsyncMock(side_effect=Exception("always fails"))
        cfg = self._make_config(max_attempts=3, base_delay=0.001)
        counter = TokenCounter()

        with self.assertRaises(Exception) as ctx:
            asyncio.run(
                _generate_with_retry(gen, {}, {}, cfg, counter)
            )
        self.assertIn("always fails", str(ctx.exception))
        self.assertEqual(gen.call_count, 3)

    def test_retry_count_matches_config(self):
        """Retry exactly max_attempts times (Property 9 validation)."""
        gen = AsyncMock(side_effect=Exception("fail"))
        cfg = self._make_config(max_attempts=5, base_delay=0.001)
        counter = TokenCounter()

        with self.assertRaises(Exception):
            asyncio.run(
                _generate_with_retry(gen, {}, {}, cfg, counter)
            )
        self.assertEqual(gen.call_count, 5)


class TestProcessTenant(unittest.TestCase):
    def _make_config(self):
        cfg = PipelineConfig()
        cfg.s3.input_bucket = "input-bucket"
        cfg.s3.output_bucket = "output-bucket"
        cfg.pipeline.max_concurrent_themes = 2
        cfg.bedrock.retry = BedrockRetryConfig(
            max_attempts=1, base_delay=0.001, max_delay=0.01,
        )
        return cfg

    @patch("app.pipeline.load_themes", return_value=None)
    def test_skipped_tenant(self, mock_load):
        cfg = self._make_config()
        clients = {"s3": MagicMock()}
        result = asyncio.run(process_tenant("tenanta", cfg, clients, "run-1"))
        self.assertEqual(result["total_kb_articles"], 0)
        self.assertEqual(result["total_rca_articles"], 0)
        self.assertTrue(result["failures"]["tenanta"]["skipped"])

    @patch("app.pipeline.record_document")
    @patch("app.pipeline.write_article_to_s3", return_value=True)
    @patch("app.pipeline.build_article_json", return_value=("uid-1", {"article": True}))
    @patch("app.pipeline.generate_rca_article", new_callable=AsyncMock,
           return_value=("rca text", "rca desc", {}))
    @patch("app.pipeline.generate_kb_article", new_callable=AsyncMock,
           return_value=("kb text", "kb desc", {}))
    @patch("app.pipeline.retrieve_similar_articles", return_value=[])
    @patch("app.pipeline.load_themes")
    def test_successful_tenant(self, mock_load, mock_retrieve, mock_kb,
                                mock_rca, mock_build, mock_write, mock_record):
        mock_load.return_value = {
            "theme1": {"keywords": ["a"], "tickets": {}},
        }
        cfg = self._make_config()
        clients = {"s3": MagicMock(), "dynamodb": MagicMock()}

        result = asyncio.run(process_tenant("tenanta", cfg, clients, "run-1"))
        self.assertEqual(result["total_kb_articles"], 1)
        self.assertEqual(result["total_rca_articles"], 1)
        self.assertEqual(result["failures"]["tenanta"]["kb_failed"], 0)
        self.assertEqual(result["failures"]["tenanta"]["rca_failed"], 0)
        # Metrics recorded for both articles
        self.assertEqual(mock_record.call_count, 2)


if __name__ == "__main__":
    unittest.main()
