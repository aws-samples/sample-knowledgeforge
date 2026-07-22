"""Unit tests for dedup handler helper functions."""
import sys
import os
import pytest
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'shared', 'python'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'dedup'))

os.environ.setdefault('AWS_REGION', 'eu-west-1')
os.environ.setdefault('AWS_DEFAULT_REGION', 'eu-west-1')

import importlib.util
_spec = importlib.util.spec_from_file_location(
    'dedup_handler',
    os.path.join(os.path.dirname(__file__), '..', 'dedup', 'handler.py')
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

floats_to_decimals = _mod.floats_to_decimals
QUALITY_CRITERIA = _mod.QUALITY_CRITERIA
_parse_freshness_date = _mod._parse_freshness_date


class TestFloatsToDecimals:

    def test_float(self):
        assert floats_to_decimals(3.14) == Decimal('3.14')

    def test_nested_dict(self):
        result = floats_to_decimals({'score': 82.5, 'name': 'test'})
        assert result['score'] == Decimal('82.5')
        assert result['name'] == 'test'

    def test_nested_list(self):
        result = floats_to_decimals([1.1, 2.2, 'hello'])
        assert result[0] == Decimal('1.1')
        assert result[2] == 'hello'

    def test_deep_nesting(self):
        result = floats_to_decimals({'a': {'b': [1.5, {'c': 2.5}]}})
        assert result['a']['b'][0] == Decimal('1.5')
        assert result['a']['b'][1]['c'] == Decimal('2.5')

    def test_int_unchanged(self):
        assert floats_to_decimals(42) == 42

    def test_none_unchanged(self):
        assert floats_to_decimals(None) is None

    def test_string_unchanged(self):
        assert floats_to_decimals('hello') == 'hello'


class TestQualityCriteria:

    def test_all_classifications_covered(self):
        expected = ['SOP', 'FAQ', 'Troubleshooting', 'RCA', 'Runbook']
        for cls in expected:
            assert cls in QUALITY_CRITERIA

    def test_criteria_not_empty(self):
        for cls, criteria in QUALITY_CRITERIA.items():
            assert len(criteria) > 20, f'Criteria too short for {cls}'


class TestParseFreshnessDate:

    def test_datetime_with_space(self):
        result = _parse_freshness_date('2025-09-18 18:49:51')
        assert result is not None
        assert result.year == 2025
        assert result.month == 9

    def test_date_only(self):
        result = _parse_freshness_date('2025-09-18')
        assert result is not None
        assert result.day == 18

    def test_iso_format(self):
        result = _parse_freshness_date('2025-09-18T18:49:51')
        assert result is not None

    def test_empty_string(self):
        assert _parse_freshness_date('') is None

    def test_none(self):
        assert _parse_freshness_date(None) is None

    def test_garbage(self):
        assert _parse_freshness_date('not-a-date') is None

    def test_timezone_aware(self):
        result = _parse_freshness_date('2025-09-18 18:49:51')
        assert result.tzinfo is not None
