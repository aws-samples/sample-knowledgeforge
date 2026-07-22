"""Shared fixtures for all Lambda unit tests."""
import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'shared', 'python'))


@pytest.fixture
def sample_article():
    """A typical ITSM_KB article in DW column format."""
    return {
        'src_kb_article_id': 'KB0010576',
        'article_title': 'SSN SMTP (Mail) relays',
        'full_text': '<h3>About SMTP</h3><p>The SSN SMTP service relays outbound email.</p>',
        'kb_type': 'text',
        'kb_category': 'be7f3520db763010bf85ce1c2996190c',
        'created_ts_utc': '2024-02-13 23:52:01',
        'last_updated_ts_utc': '2025-09-18 18:49:51',
        'status': 'published',
        'active': 'true',
        'kb_author': 'admin',
        'sys_domain': 'global',
        'sys_class_name': 'kb_knowledge',
        'kb_valid_to_ts': '2100-01-01',
        'description': '',
        'can_read_user_criteria': '',
        'language': 'en',
    }


@pytest.fixture
def sample_sn_payload():
    """Pipeline output payload as stored in S3 generated/{article_id}.json."""
    return {
        'src_kb_article_id': 'KB0010576',
        'source_system': 'ITSM_KB',
        'enriched_text': '<p>Enriched content</p>',
        'classification': 'SOP',
        'classification_confidence': 92,
        'is_duplicate': False,
        'quality_score_before': 55.3,
        'quality_score_after': 82.1,
        'quality_passed': True,
        'quality_issues': ['Missing escalation path'],
        'enrichment_summary': 'Added diagnostic steps.',
    }
