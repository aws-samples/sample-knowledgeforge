"""Unit tests for dispatcher handler.

Tests:
  - Malformed SQS message body → raises exception
  - Step Function returns SUCCEEDED → function returns normally
  - Step Function returns FAILED → function raises RuntimeError
  - Step Function returns TIMED_OUT → function raises RuntimeError
"""

import json
import os
import sys
import types
import pytest
from unittest.mock import patch, MagicMock

# Mock logger before importing handler
_logger_mod = types.ModuleType('logger')
_logger_mod.get_logger = MagicMock(return_value=MagicMock())
sys.modules.setdefault('logger', _logger_mod)

os.environ.update({
    'STATE_MACHINE_ARN': 'arn:aws:states:eu-west-1:123456789:stateMachine:test',
    'AWS_REGION': 'eu-west-1',
})

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from dispatcher.handler import (
    lambda_handler,
    _handle_terminal_state,
)
import dispatcher.handler as handler_module


def _make_sqs_event(body):
    """Build a minimal SQS event with a single record."""
    return {
        'Records': [{
            'body': json.dumps(body) if isinstance(body, dict) else body,
        }]
    }


class TestMalformedMessage:
    """Malformed SQS message body raises an exception."""

    def test_malformed_json_raises(self):
        event = {'Records': [{'body': 'not-valid-json{{{'}]}
        with pytest.raises(json.JSONDecodeError):
            lambda_handler(event, None)

    @patch.object(handler_module, 'sfn')
    def test_missing_files_key_raises(self, mock_sfn):
        event = _make_sqs_event({'batch_id': 'b1'})
        with pytest.raises(KeyError):
            lambda_handler(event, None)


class TestSucceeded:
    """Step Function returns SUCCEEDED → function returns normally."""

    @patch.object(handler_module, 'time')
    @patch.object(handler_module, 'sfn')
    def test_succeeded_returns_normally(self, mock_sfn, mock_time):
        mock_sfn.start_execution.return_value = {
            'executionArn': 'arn:aws:states:eu-west-1:123:execution:test:run-1',
        }
        mock_sfn.describe_execution.return_value = {
            'status': 'SUCCEEDED',
            'executionArn': 'arn:aws:states:eu-west-1:123:execution:test:run-1',
        }

        event = _make_sqs_event({
            'batch_id': 'b1',
            'files': [{'tenant_id': 't1', 'article_id': 'a1',
                        'source_file_path': 's3://b/k', 'reason': 'NEW'}],
        })

        result = lambda_handler(event, None)

        assert result['status'] == 'SUCCEEDED'
        assert result['batch_id'] == 'b1'
        mock_sfn.start_execution.assert_called_once()


class TestFailed:
    """Step Function returns FAILED → function raises RuntimeError."""

    @patch.object(handler_module, 'time')
    @patch.object(handler_module, 'sfn')
    def test_failed_raises_runtime_error(self, mock_sfn, mock_time):
        exec_arn = 'arn:aws:states:eu-west-1:123:execution:test:run-2'
        mock_sfn.start_execution.return_value = {'executionArn': exec_arn}
        mock_sfn.describe_execution.return_value = {
            'status': 'FAILED',
            'executionArn': exec_arn,
            'error': 'States.TaskFailed',
            'cause': 'Lambda returned error',
        }

        event = _make_sqs_event({
            'batch_id': 'b2',
            'files': [{'tenant_id': 't1', 'article_id': 'a1',
                        'source_file_path': 's3://b/k', 'reason': 'NEW'}],
        })

        with pytest.raises(RuntimeError, match='FAILED'):
            lambda_handler(event, None)


class TestTimedOut:
    """Step Function returns TIMED_OUT → function raises RuntimeError."""

    @patch.object(handler_module, 'time')
    @patch.object(handler_module, 'sfn')
    def test_timed_out_raises_runtime_error(self, mock_sfn, mock_time):
        exec_arn = 'arn:aws:states:eu-west-1:123:execution:test:run-3'
        mock_sfn.start_execution.return_value = {'executionArn': exec_arn}
        mock_sfn.describe_execution.return_value = {
            'status': 'TIMED_OUT',
            'executionArn': exec_arn,
            'error': 'States.Timeout',
            'cause': 'Execution timed out',
        }

        event = _make_sqs_event({
            'batch_id': 'b3',
            'files': [{'tenant_id': 't1', 'article_id': 'a1',
                        'source_file_path': 's3://b/k', 'reason': 'NEW'}],
        })

        with pytest.raises(RuntimeError, match='TIMED_OUT'):
            lambda_handler(event, None)
