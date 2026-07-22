"""Unit tests for file_enumerator handler.

Tests:
  - Empty bucket (zero tenants) → zero SQS messages sent
  - Single tenant with no changed files → zero SQS messages
  - SQS SendMessage failure for one batch → remaining batches still sent
  - Exact batch boundary (N files with batch_size=N → exactly 1 batch)
"""

import json
import os
import sys
import types
import pytest
from unittest.mock import patch, MagicMock

# ── Bootstrap: mock heavy external deps before importing handler ──────────

# Create fake change_detection package so the handler can import from it
_cd_mod = types.ModuleType('change_detection')
_cd_handler = types.ModuleType('change_detection.handler')
_cd_handler._init_config = MagicMock()
_cd_handler._ingest_json_articles = MagicMock(return_value=([], {}, set()))
_cd_handler._handle_deletes = MagicMock(return_value=([], 0))
_cd_handler.get_existing_records = MagicMock(return_value={})
_cd_handler.TABLE_NAME = 'mock-table'
_cd_mod.handler = _cd_handler
sys.modules['change_detection'] = _cd_mod
sys.modules['change_detection.handler'] = _cd_handler

# Mock logger
_logger_mod = types.ModuleType('logger')
_logger_mod.get_logger = MagicMock(return_value=MagicMock())
sys.modules['logger'] = _logger_mod

# Set env vars before import
os.environ.update({
    'SOURCE_BUCKET': 'test-source-bucket',
    'PIPELINE_BUCKET': 'test-pipeline-bucket',
    'QUEUE_URL': 'https://sqs.eu-west-1.amazonaws.com/123/q.fifo',
    'BATCH_SIZE': '50',
    'AWS_REGION': 'eu-west-1',
})

# Add lambda dir to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

# Now import the handler
from file_enumerator.handler import (
    lambda_handler,
    _create_batches,
    _send_batches_to_sqs,
)
import file_enumerator.handler as handler_module


class TestEmptyBucket:
    """When the source bucket has no tenant prefixes, zero SQS messages are sent."""

    @patch.object(handler_module, 'sqs')
    @patch.object(handler_module, '_discover_tenants', return_value=[])
    def test_empty_bucket_sends_zero_messages(self, mock_discover, mock_sqs):
        result = lambda_handler({}, None)

        assert result['tenants_processed'] == 0
        assert result['total_changed_files'] == 0
        assert result['batches_sent'] == 0
        mock_sqs.send_message.assert_not_called()


class TestSingleTenantNoChanges:
    """When a single tenant has no changed files, zero SQS messages are sent."""

    @patch.object(handler_module, 'sqs')
    @patch.object(handler_module, '_process_tenant', return_value=[])
    @patch.object(handler_module, '_discover_tenants', return_value=['tenant_a'])
    def test_single_tenant_no_changes_sends_zero_messages(
        self, mock_discover, mock_process, mock_sqs
    ):
        result = lambda_handler({}, None)

        assert result['tenants_processed'] == 1
        assert result['total_changed_files'] == 0
        assert result['batches_sent'] == 0
        mock_sqs.send_message.assert_not_called()


class TestSqsSendFailureContinues:
    """When SQS SendMessage fails for one batch, remaining batches are still sent."""

    @patch.object(handler_module, 'sqs')
    def test_sqs_failure_continues_remaining_batches(self, mock_sqs):
        # First call fails, second succeeds
        mock_sqs.send_message.side_effect = [
            Exception('SQS throttled'),
            {'MessageId': 'msg-2'},
        ]

        batches = [
            [{'tenant_id': 't1', 'article_id': 'a1', 'source_file_path': 's3://b/k1', 'reason': 'NEW'}],
            [{'tenant_id': 't1', 'article_id': 'a2', 'source_file_path': 's3://b/k2', 'reason': 'UPDATED'}],
        ]

        sent = _send_batches_to_sqs(batches)

        assert sent == 1
        assert mock_sqs.send_message.call_count == 2


class TestExactBatchBoundary:
    """When file count equals batch_size, exactly 1 batch is produced."""

    def test_exact_batch_boundary(self):
        files = [
            {'tenant_id': f't{i}', 'article_id': f'a{i}',
             'source_file_path': f's3://b/k{i}', 'reason': 'NEW'}
            for i in range(50)
        ]
        batches = _create_batches(files, 50)
        assert len(batches) == 1
        assert len(batches[0]) == 50

    def test_one_over_batch_boundary(self):
        files = [
            {'tenant_id': f't{i}', 'article_id': f'a{i}',
             'source_file_path': f's3://b/k{i}', 'reason': 'NEW'}
            for i in range(51)
        ]
        batches = _create_batches(files, 50)
        assert len(batches) == 2
        assert len(batches[0]) == 50
        assert len(batches[1]) == 1

    def test_empty_list_produces_no_batches(self):
        batches = _create_batches([], 50)
        assert batches == []
