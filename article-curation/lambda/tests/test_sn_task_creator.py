"""Unit tests for sn_task_creator business logic."""
import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'shared', 'python'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'sn_task_creator'))

os.environ.setdefault('AWS_REGION', 'eu-west-1')
os.environ.setdefault('AWS_DEFAULT_REGION', 'eu-west-1')
os.environ.setdefault('S3_BUCKET', 'test-bucket')
os.environ.setdefault('ARTICLE_TABLE', 'test-article-table')
os.environ.setdefault('JOB_STATUS_TABLE', 'test-job-table')

import importlib.util
_spec = importlib.util.spec_from_file_location(
    'sn_task_creator_handler',
    os.path.join(os.path.dirname(__file__), '..', 'sn_task_creator', 'handler.py')
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

build_kb_score = _mod.build_kb_score
build_kb_json_existing = _mod.build_kb_json_existing


class TestBuildKbScore:

    def test_all_fields(self, sample_sn_payload):
        score = build_kb_score(sample_sn_payload)
        assert score['classification'] == 'SOP'
        assert score['classification_confidence'] == 92
        assert score['is_duplicate'] is False
        assert score['quality_score_before'] == 55.3
        assert score['quality_score_after'] == 82.1
        assert score['quality_passed'] is True
        assert len(score['quality_issues']) == 1
        assert score['enrichment_summary'] == 'Added diagnostic steps.'

    def test_empty_payload(self):
        score = build_kb_score({})
        assert score['classification'] == ''
        assert score['classification_confidence'] == 0
        assert score['is_duplicate'] is False
        assert score['quality_issues'] == []
        assert score['enrichment_summary'] == ''


class TestBuildKbJsonExisting:

    def test_itsm_kb_payload(self, sample_sn_payload):
        result = build_kb_json_existing(sample_sn_payload)
        assert result['article_id'] == 'KB0010576'
        assert result['source_system'] == 'ITSM_KB'
        assert result['enriched_text'] == '<p>Enriched content</p>'

    def test_no_sys_id(self, sample_sn_payload):
        result = build_kb_json_existing(sample_sn_payload)
        assert 'sys_id' not in result

    def test_minimal_fields(self):
        result = build_kb_json_existing({})
        assert result['article_id'] == ''
        assert result['source_system'] == 'ITSM_KB'
        assert result['enriched_text'] == ''
