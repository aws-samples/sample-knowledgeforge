"""Property-based tests for file_enumerator handler.

Uses hypothesis to verify correctness properties from the design document.

Properties tested:
  - Property 1: File discovery completeness
  - Property 2: Change detection filtering
  - Property 3: Batch partitioning
  - Property 4: SQS message correctness
"""

import json
import math
import os
import sys
import types
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings, assume
from hypothesis import strategies as st

# ── Bootstrap: mock heavy external deps before importing handler ──────────

_cd_mod = types.ModuleType('change_detection')
_cd_handler = types.ModuleType('change_detection.handler')
_cd_handler._init_config = MagicMock()
_cd_handler._ingest_json_articles = MagicMock(return_value=([], {}, set()))
_cd_handler._handle_deletes = MagicMock(return_value=([], 0))
_cd_handler.get_existing_records = MagicMock(return_value={})
_cd_handler.TABLE_NAME = 'mock-table'
_cd_mod.handler = _cd_handler
sys.modules.setdefault('change_detection', _cd_mod)
sys.modules.setdefault('change_detection.handler', _cd_handler)

_logger_mod = types.ModuleType('logger')
_logger_mod.get_logger = MagicMock(return_value=MagicMock())
sys.modules.setdefault('logger', _logger_mod)

os.environ.update({
    'SOURCE_BUCKET': 'test-source-bucket',
    'PIPELINE_BUCKET': 'test-pipeline-bucket',
    'QUEUE_URL': 'https://sqs.eu-west-1.amazonaws.com/123/q.fifo',
    'BATCH_SIZE': '50',
    'AWS_REGION': 'eu-west-1',
})

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from file_enumerator.handler import (
    _create_batches,
    _discover_tenants,
    _send_batches_to_sqs,
)
import file_enumerator.handler as handler_module


# ── Strategies ────────────────────────────────────────────────────────────────

# Tenant IDs: non-empty alphanumeric strings (no dots or slashes)
tenant_id_st = st.text(
    alphabet=st.sampled_from('abcdefghijklmnopqrstuvwxyz0123456789_'),
    min_size=1, max_size=20,
).filter(lambda t: not t.startswith('.'))

reason_st = st.sampled_from(['NEW', 'UPDATED', 'RETRY_INCOMPLETE', 'ORPHANED_DUPLICATE'])

file_ref_st = st.fixed_dictionaries({
    'tenant_id': tenant_id_st,
    'article_id': st.text(min_size=1, max_size=30, alphabet='abcdefghijklmnopqrstuvwxyz0123456789_'),
    'source_file_path': st.text(min_size=5, max_size=80, alphabet='abcdefghijklmnopqrstuvwxyz0123456789_/:.'),
    'reason': reason_st,
})


# ── Property 1: File discovery completeness ───────────────────────────────────
# Feature: sqs-tenant-dispatcher, Property 1: File discovery completeness
# **Validates: Requirements 2.1, 2.2**


class TestFileDiscoveryCompleteness:
    """For any set of tenant prefixes in the Source_Bucket, _discover_tenants
    shall discover every tenant prefix."""

    @given(tenant_list=st.lists(tenant_id_st, min_size=0, max_size=20, unique=True))
    @settings(max_examples=100)
    def test_discover_tenants_finds_all_prefixes(self, tenant_list):
        """Mock S3 paginator to return random tenant prefixes and verify
        _discover_tenants returns all of them."""
        # Build paginator response pages (single page for simplicity)
        common_prefixes = [{'Prefix': f'{t}/'} for t in tenant_list]
        pages = [{'CommonPrefixes': common_prefixes}] if common_prefixes else [{}]

        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = pages

        mock_s3 = MagicMock()
        mock_s3.get_paginator.return_value = mock_paginator

        with patch.object(handler_module, 's3', mock_s3):
            discovered = _discover_tenants()

        assert set(discovered) == set(tenant_list)
        assert len(discovered) == len(tenant_list)


# ── Property 2: Change detection filtering ────────────────────────────────────
# Feature: sqs-tenant-dispatcher, Property 2: Change detection filtering
# **Validates: Requirements 2.4**


class TestChangeDetectionFiltering:
    """Only changed files appear in output — unchanged files are excluded."""

    @given(
        tenants=st.lists(tenant_id_st, min_size=1, max_size=5, unique=True),
        changed_per_tenant=st.lists(
            st.integers(min_value=0, max_value=10),
            min_size=1, max_size=5,
        ),
    )
    @settings(max_examples=100)
    def test_only_changed_files_in_output(self, tenants, changed_per_tenant):
        """Mock _process_tenant to return a known subset of changed files
        and verify only those appear in the final output."""
        # Align lengths
        changed_per_tenant = changed_per_tenant[:len(tenants)]
        while len(changed_per_tenant) < len(tenants):
            changed_per_tenant.append(0)

        expected_changed = []
        tenant_results = {}
        for tenant, count in zip(tenants, changed_per_tenant):
            files = [
                {
                    'tenant_id': tenant,
                    'article_id': f'{tenant}_art_{i}',
                    'source_file_path': f's3://bucket/{tenant}/raw/{i}.html',
                    'reason': 'NEW',
                }
                for i in range(count)
            ]
            tenant_results[tenant] = files
            expected_changed.extend(files)

        def mock_process(tid):
            return tenant_results.get(tid, [])

        mock_sqs = MagicMock()
        mock_sqs.send_message.return_value = {'MessageId': 'msg-1'}

        with patch.object(handler_module, '_discover_tenants', return_value=tenants), \
             patch.object(handler_module, '_process_tenant', side_effect=mock_process), \
             patch.object(handler_module, 'sqs', mock_sqs):
            from file_enumerator.handler import lambda_handler
            result = lambda_handler({}, None)

        assert result['total_changed_files'] == len(expected_changed)


# ── Property 3: Batch partitioning ────────────────────────────────────────────
# Feature: sqs-tenant-dispatcher, Property 3: Batch partitioning
# **Validates: Requirements 2.5, 2.6**


class TestBatchPartitioning:
    """For any list of N changed files and batch size B > 0,
    _create_batches produces ceil(N/B) batches with correct sizes."""

    @given(
        n=st.integers(min_value=0, max_value=500),
        batch_size=st.integers(min_value=1, max_value=100),
    )
    @settings(max_examples=100)
    def test_batch_count_and_sizes(self, n, batch_size):
        files = [
            {
                'tenant_id': f't{i}',
                'article_id': f'a{i}',
                'source_file_path': f's3://b/k{i}',
                'reason': 'NEW',
            }
            for i in range(n)
        ]

        batches = _create_batches(files, batch_size)

        if n == 0:
            assert batches == []
            return

        # Correct number of batches
        expected_count = math.ceil(n / batch_size)
        assert len(batches) == expected_count

        # All batches except last have exactly batch_size items
        for b in batches[:-1]:
            assert len(b) == batch_size

        # Last batch has between 1 and batch_size items
        assert 1 <= len(batches[-1]) <= batch_size

        # Union of all batches equals original list (no duplicates, no omissions)
        flattened = [item for batch in batches for item in batch]
        assert flattened == files


# ── Property 4: SQS message correctness ──────────────────────────────────────
# Feature: sqs-tenant-dispatcher, Property 4: SQS message correctness
# **Validates: Requirements 2.7, 2.8, 2.9, 3.7, 9.1**


class TestSqsMessageCorrectness:
    """Every SQS message has correct structure, same MessageGroupId,
    and unique MessageDeduplicationId."""

    @given(
        batch_count=st.integers(min_value=1, max_value=20),
        files_per_batch=st.integers(min_value=1, max_value=10),
    )
    @settings(max_examples=100)
    def test_sqs_messages_structure_and_uniqueness(self, batch_count, files_per_batch):
        batches = [
            [
                {
                    'tenant_id': f't{b}_{f}',
                    'article_id': f'a{b}_{f}',
                    'source_file_path': f's3://bucket/t{b}/raw/{f}.html',
                    'reason': 'NEW',
                }
                for f in range(files_per_batch)
            ]
            for b in range(batch_count)
        ]

        sent_calls = []
        mock_sqs = MagicMock()

        def capture_send(**kwargs):
            sent_calls.append(kwargs)
            return {'MessageId': f'msg-{len(sent_calls)}'}

        mock_sqs.send_message.side_effect = capture_send

        with patch.object(handler_module, 'sqs', mock_sqs):
            sent_count = _send_batches_to_sqs(batches)

        assert sent_count == batch_count
        assert len(sent_calls) == batch_count

        dedup_ids = set()
        for call in sent_calls:
            # (a) Message body has correct structure
            body = json.loads(call['MessageBody'])
            assert 'files' in body
            assert 'batch_id' in body
            for file_ref in body['files']:
                assert 'tenant_id' in file_ref
                assert 'article_id' in file_ref
                assert 'source_file_path' in file_ref
                assert 'reason' in file_ref

            # (b) Same MessageGroupId across all messages
            assert call['MessageGroupId'] == 'pipeline-batch'

            # (c) Unique MessageDeduplicationId
            dedup_id = call['MessageDeduplicationId']
            assert dedup_id not in dedup_ids
            dedup_ids.add(dedup_id)

        assert len(dedup_ids) == batch_count
