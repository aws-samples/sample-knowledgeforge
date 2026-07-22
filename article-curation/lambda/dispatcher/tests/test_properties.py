"""Property-based tests for dispatcher handler.

Uses hypothesis to verify correctness properties from the design document.

Properties tested:
  - Property 5: Dispatcher message parsing and forwarding
  - Property 6: Execution name uniqueness
  - Property 7: Polling and terminal state handling
"""

import json
import os
import sys
import types
from unittest.mock import MagicMock, patch, call

import pytest
from hypothesis import given, settings, assume
from hypothesis import strategies as st

# ── Bootstrap: mock heavy external deps before importing handler ──────────

_logger_mod = types.ModuleType('logger')
_logger_mod.get_logger = MagicMock(return_value=MagicMock())
sys.modules.setdefault('logger', _logger_mod)

os.environ.update({
    'STATE_MACHINE_ARN': 'arn:aws:states:eu-west-1:123456789:stateMachine:test',
    'AWS_REGION': 'eu-west-1',
})

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from dispatcher.handler import (
    _generate_execution_name,
    _handle_terminal_state,
    _poll_execution,
)
import dispatcher.handler as handler_module


# ── Strategies ────────────────────────────────────────────────────────────────

tenant_id_st = st.text(
    alphabet=st.sampled_from('abcdefghijklmnopqrstuvwxyz0123456789_'),
    min_size=1, max_size=20,
)

reason_st = st.sampled_from(['NEW', 'UPDATED', 'RETRY_INCOMPLETE', 'ORPHANED_DUPLICATE'])

file_ref_st = st.fixed_dictionaries({
    'tenant_id': tenant_id_st,
    'article_id': st.text(min_size=1, max_size=30, alphabet='abcdefghijklmnopqrstuvwxyz0123456789_'),
    'source_file_path': st.text(min_size=5, max_size=80, alphabet='abcdefghijklmnopqrstuvwxyz0123456789_/:.'),
    'reason': reason_st,
})


# ── Property 5: Dispatcher message parsing and forwarding ─────────────────────
# Feature: sqs-tenant-dispatcher, Property 5: Dispatcher message parsing and forwarding
# **Validates: Requirements 4.3, 4.4**


class TestMessageParsingAndForwarding:
    """For any valid SQS message body containing a files array,
    the Dispatcher shall call StartExecution with the same file references."""

    @given(files=st.lists(file_ref_st, min_size=1, max_size=20))
    @settings(max_examples=100)
    def test_start_execution_receives_same_files(self, files):
        batch_id = 'test-batch-001'
        event = {
            'Records': [{
                'body': json.dumps({'batch_id': batch_id, 'files': files}),
            }]
        }

        exec_arn = 'arn:aws:states:eu-west-1:123:execution:test:run-1'

        mock_sfn = MagicMock()
        mock_sfn.start_execution.return_value = {'executionArn': exec_arn}
        mock_sfn.describe_execution.return_value = {
            'status': 'SUCCEEDED',
            'executionArn': exec_arn,
        }

        mock_time = MagicMock()

        with patch.object(handler_module, 'sfn', mock_sfn), \
             patch.object(handler_module, 'time', mock_time):
            from dispatcher.handler import lambda_handler
            lambda_handler(event, None)

        # Verify StartExecution was called with the same files
        start_call = mock_sfn.start_execution.call_args
        sfn_input = json.loads(start_call.kwargs['input'])
        assert sfn_input['files'] == files
        assert sfn_input['batch_id'] == batch_id


# ── Property 6: Execution name uniqueness ─────────────────────────────────────
# Feature: sqs-tenant-dispatcher, Property 6: Execution name uniqueness
# **Validates: Requirements 4.5**


class TestExecutionNameUniqueness:
    """For any number of invocations, generated execution names are distinct."""

    @given(count=st.integers(min_value=2, max_value=200))
    @settings(max_examples=100)
    def test_execution_names_are_unique(self, count):
        names = set()
        for _ in range(count):
            name = _generate_execution_name()
            names.add(name)

        assert len(names) == count


# ── Property 7: Polling and terminal state handling ───────────────────────────
# Feature: sqs-tenant-dispatcher, Property 7: Polling and terminal state handling
# **Validates: Requirements 4.6, 4.7, 4.8, 9.2**


class TestPollingAndTerminalState:
    """For any sequence of RUNNING states followed by a terminal state,
    the Dispatcher polls correctly and handles the terminal state."""

    @given(
        running_count=st.integers(min_value=0, max_value=10),
        terminal_state=st.sampled_from(['SUCCEEDED', 'FAILED', 'TIMED_OUT', 'ABORTED']),
    )
    @settings(max_examples=100)
    def test_poll_and_handle_terminal_state(self, running_count, terminal_state):
        exec_arn = 'arn:aws:states:eu-west-1:123:execution:test:run-poll'

        # Build sequence: N RUNNING responses, then 1 terminal response
        responses = [
            {'status': 'RUNNING', 'executionArn': exec_arn}
            for _ in range(running_count)
        ]
        terminal_response = {
            'status': terminal_state,
            'executionArn': exec_arn,
        }
        if terminal_state != 'SUCCEEDED':
            terminal_response['error'] = 'SomeError'
            terminal_response['cause'] = 'Some cause'
        responses.append(terminal_response)

        mock_sfn = MagicMock()
        mock_sfn.describe_execution.side_effect = responses

        mock_time = MagicMock()

        with patch.object(handler_module, 'sfn', mock_sfn), \
             patch.object(handler_module, 'time', mock_time):
            result = _poll_execution(exec_arn)

        # Verify polling happened the right number of times
        assert mock_sfn.describe_execution.call_count == running_count + 1

        # Verify time.sleep was called for each RUNNING state
        assert mock_time.sleep.call_count == running_count

        # Verify terminal state handling
        assert result['status'] == terminal_state

        if terminal_state == 'SUCCEEDED':
            # Should return normally
            _handle_terminal_state(result)  # no exception
        else:
            # Should raise RuntimeError
            with pytest.raises(RuntimeError, match=terminal_state):
                _handle_terminal_state(result)
