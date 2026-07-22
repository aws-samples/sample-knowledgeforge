"""Unit tests for file_enumerator business logic.
Uses monkeypatch to set env vars before importing the handler.
"""
import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'shared', 'python'))

# Set env vars before importing handler
os.environ.setdefault('AWS_REGION', 'eu-west-1')
os.environ.setdefault('AWS_DEFAULT_REGION', 'eu-west-1')
os.environ.setdefault('SOURCE_BUCKET', 'test-bucket')
os.environ.setdefault('PIPELINE_BUCKET', 'test-pipeline')
os.environ.setdefault('QUEUE_URL', 'https://sqs.eu-west-1.amazonaws.com/123/test.fifo')
os.environ.setdefault('SOURCE_PREFIX', 'tenant_partitioning/itsm/snow/kb_articles')

# Use importlib to load the specific handler module by path
import importlib.util
_spec = importlib.util.spec_from_file_location(
    'file_enumerator_handler',
    os.path.join(os.path.dirname(__file__), '..', 'file_enumerator', 'handler.py')
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_parse_json_payload = _mod._parse_json_payload
_compute_content_hash = _mod._compute_content_hash
_sanitize_filename = _mod._sanitize_filename
_create_tenant_batches = _mod._create_tenant_batches
_extract_metadata = _mod._extract_metadata
_tenant_s3_prefix = _mod._tenant_s3_prefix


class TestParseJsonPayload:

    def test_single_article(self):
        data = {'src_kb_article_id': 'KB001', 'full_text': '<p>test</p>'}
        assert len(_parse_json_payload(data)) == 1

    def test_array(self):
        data = [{'src_kb_article_id': 'KB001'}, {'src_kb_article_id': 'KB002'}]
        assert len(_parse_json_payload(data)) == 2

    def test_records_wrapper(self):
        assert len(_parse_json_payload({'records': [{'src_kb_article_id': 'KB001'}]})) == 1

    def test_result_array(self):
        assert len(_parse_json_payload({'result': [{'src_kb_article_id': 'KB001'}]})) == 1

    def test_result_data(self):
        assert len(_parse_json_payload({'result': {'data': [{'src_kb_article_id': 'KB001'}]}})) == 1

    def test_unrecognized(self):
        assert len(_parse_json_payload({'random': 'value'})) == 0

    def test_empty_list(self):
        assert len(_parse_json_payload([])) == 0


class TestContentHash:

    def test_deterministic(self):
        assert _compute_content_hash('hello') == _compute_content_hash('hello')

    def test_different_input(self):
        assert _compute_content_hash('a') != _compute_content_hash('b')

    def test_sha256_length(self):
        assert len(_compute_content_hash('test')) == 64


class TestSanitizeFilename:

    def test_spaces(self):
        assert _sanitize_filename('Hello World') == 'Hello_World'

    def test_special_chars(self):
        result = _sanitize_filename('Test: file/name')
        assert '/' not in result
        assert ':' not in result

    def test_max_length(self):
        assert len(_sanitize_filename('a' * 200, max_length=80)) <= 80

    def test_empty(self):
        assert _sanitize_filename('') == ''


class TestCreateTenantBatches:

    def test_single_tenant(self):
        files = [{'tenant_id': 'alpha', 'article_id': f'a{i}'} for i in range(10)]
        batches = _create_tenant_batches(files, 5)
        assert len(batches) == 2
        assert all(tid == 'alpha' for tid, _ in batches)
        assert len(batches[0][1]) == 5
        assert len(batches[1][1]) == 5

    def test_multi_tenant_separate_batches(self):
        files = [
            {'tenant_id': 'alpha', 'article_id': 'a1'},
            {'tenant_id': 'alpha', 'article_id': 'a2'},
            {'tenant_id': 'beta', 'article_id': 'b1'},
        ]
        batches = _create_tenant_batches(files, 10)
        assert len(batches) == 2  # one per tenant
        tenant_ids = {tid for tid, _ in batches}
        assert tenant_ids == {'alpha', 'beta'}

    def test_remainder(self):
        files = [{'tenant_id': 'alpha', 'article_id': f'a{i}'} for i in range(7)]
        batches = _create_tenant_batches(files, 3)
        assert len(batches) == 3
        assert len(batches[2][1]) == 1

    def test_empty(self):
        assert _create_tenant_batches([], 10) == []

    def test_zero_size(self):
        assert _create_tenant_batches([{'tenant_id': 'a'}], 0) == []


class TestExtractMetadata:

    def test_all_fields(self, sample_article):
        meta = _extract_metadata(sample_article)
        assert meta['src_kb_article_id'] == 'KB0010576'
        assert meta['article_title'] == 'SSN SMTP (Mail) relays'
        assert meta['language'] == 'en'
        assert meta['active'] == 'true'
        assert meta['status'] == 'published'

    def test_missing_fields(self):
        meta = _extract_metadata({'src_kb_article_id': 'KB001'})
        assert meta['article_title'] == ''
        assert meta['kb_category'] == ''


class TestTenantS3Prefix:

    def test_builds_correct_path(self):
        result = _tenant_s3_prefix('acme')
        assert 'acme' in result
        assert result.endswith('/')
