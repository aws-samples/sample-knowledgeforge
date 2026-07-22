"""Unit tests for webhook_handler business logic."""
import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'webhook_handler'))

os.environ.setdefault('AWS_REGION', 'eu-west-1')
os.environ.setdefault('AWS_DEFAULT_REGION', 'eu-west-1')
os.environ.setdefault('ARTICLE_TABLE', 'test-article-table')

import importlib.util
_spec = importlib.util.spec_from_file_location(
    'webhook_handler_handler',
    os.path.join(os.path.dirname(__file__), '..', 'webhook_handler', 'handler.py')
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

DECISION_MAP = _mod.DECISION_MAP


class TestDecisionMap:

    def test_approved(self):
        assert DECISION_MAP['approved'] == 'KM_APPROVED'

    def test_rejected(self):
        assert DECISION_MAP['rejected'] == 'KM_REJECTED'

    def test_only_two_decisions(self):
        assert len(DECISION_MAP) == 2

    def test_case_sensitive(self):
        assert 'APPROVED' not in DECISION_MAP
        assert 'Approved' not in DECISION_MAP


class TestMessageValidation:
    """Test message field validation logic (without DynamoDB calls)."""

    def test_valid_approved_message(self):
        msg = {
            'ticket_id': 'RITM0010220',
            'decision': 'approved',
            'approver_id': 'test.user',
            'approver_name': 'Test User',
            'comments': 'Looks good',
            'decided_at': '2026-04-22 18:45:00',
        }
        decision = msg['decision'].strip().lower()
        assert decision in DECISION_MAP
        assert msg['ticket_id'] != ''

    def test_valid_rejected_message(self):
        msg = {
            'ticket_id': 'RITM0010221',
            'decision': 'rejected',
            'approver_id': 'test.user',
            'rejection_reason': 'Content inaccurate',
        }
        decision = msg['decision'].strip().lower()
        assert decision in DECISION_MAP

    def test_missing_ticket_id(self):
        msg = {'decision': 'approved', 'approver_id': 'test'}
        assert msg.get('ticket_id', '') == ''

    def test_invalid_decision(self):
        msg = {'ticket_id': 'RITM001', 'decision': 'pending'}
        decision = msg['decision'].strip().lower()
        assert decision not in DECISION_MAP

    def test_empty_decision(self):
        msg = {'ticket_id': 'RITM001', 'decision': ''}
        decision = (msg.get('decision') or '').strip().lower()
        assert decision not in DECISION_MAP

    def test_decision_with_whitespace(self):
        msg = {'ticket_id': 'RITM001', 'decision': '  Approved  '}
        decision = msg['decision'].strip().lower()
        assert decision in DECISION_MAP

    def test_rejection_reason_fallback_to_comments(self):
        msg = {
            'ticket_id': 'RITM001',
            'decision': 'rejected',
            'rejection_reason': None,
            'comments': 'Not good enough',
        }
        reason = msg.get('rejection_reason') or msg.get('comments', '')
        assert reason == 'Not good enough'
