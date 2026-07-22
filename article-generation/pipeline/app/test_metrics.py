"""Unit tests for pipeline.app.metrics."""

import logging
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from pipeline.app.metrics import (
    record_document,
    record_run_complete,
    record_run_failed,
    record_run_start,
)


@pytest.fixture
def table():
    """Return a mock DynamoDB Table resource."""
    return MagicMock()


# ── record_run_start ────────────────────────────────────────────────


class TestRecordRunStart:
    def test_writes_running_record(self, table):
        record_run_start(table, "run-1", "tenanta", "2025-01-15T10:00:00+00:00")

        table.put_item.assert_called_once_with(Item={
            "run_id": "run-1",
            "record_type": "RUN",
            "status": "RUNNING",
            "start_time": "2025-01-15T10:00:00+00:00",
            "tenant": "tenanta",
        })

    def test_logs_error_on_failure(self, table, caplog):
        table.put_item.side_effect = Exception("DynamoDB down")

        with caplog.at_level(logging.ERROR):
            record_run_start(table, "run-1", "tenanta", "2025-01-15T10:00:00+00:00")

        assert "Failed to write run-start record" in caplog.text
        assert "run-1" in caplog.text

    def test_does_not_raise_on_failure(self, table):
        table.put_item.side_effect = Exception("DynamoDB down")
        # Should not raise
        record_run_start(table, "run-1", "tenanta", "2025-01-15T10:00:00+00:00")


# ── record_run_complete ─────────────────────────────────────────────


class TestRecordRunComplete:
    def test_updates_with_completed_status_and_totals(self, table):
        result = {
            "tenants_processed": 3,
            "total_kb_articles": 10,
            "total_rca_articles": 10,
            "token_usage": {"input_tokens": 500, "output_tokens": 300, "total_tokens": 800},
        }
        record_run_complete(table, "run-2", result, "2025-01-15T11:00:00+00:00")

        table.update_item.assert_called_once()
        call_kwargs = table.update_item.call_args[1]
        assert call_kwargs["Key"] == {"run_id": "run-2", "record_type": "RUN"}
        assert call_kwargs["ExpressionAttributeValues"][":status"] == "COMPLETED"
        assert call_kwargs["ExpressionAttributeValues"][":end_time"] == "2025-01-15T11:00:00+00:00"
        assert call_kwargs["ExpressionAttributeValues"][":tp"] == 3
        assert call_kwargs["ExpressionAttributeValues"][":kb"] == 10
        assert call_kwargs["ExpressionAttributeValues"][":rca"] == 10
        assert call_kwargs["ExpressionAttributeValues"][":tu"]["total_tokens"] == 800

    def test_defaults_missing_result_keys_to_zero(self, table):
        record_run_complete(table, "run-2", {}, "2025-01-15T11:00:00+00:00")

        call_kwargs = table.update_item.call_args[1]
        assert call_kwargs["ExpressionAttributeValues"][":tp"] == 0
        assert call_kwargs["ExpressionAttributeValues"][":kb"] == 0
        assert call_kwargs["ExpressionAttributeValues"][":rca"] == 0
        assert call_kwargs["ExpressionAttributeValues"][":tu"] == {}

    def test_does_not_raise_on_failure(self, table):
        table.update_item.side_effect = Exception("DynamoDB down")
        record_run_complete(table, "run-2", {}, "2025-01-15T11:00:00+00:00")


# ── record_run_failed ───────────────────────────────────────────────


class TestRecordRunFailed:
    def test_updates_with_failed_status_and_error(self, table):
        record_run_failed(table, "run-3", ValueError("bad input"), "2025-01-15T12:00:00+00:00")

        table.update_item.assert_called_once()
        call_kwargs = table.update_item.call_args[1]
        assert call_kwargs["Key"] == {"run_id": "run-3", "record_type": "RUN"}
        assert call_kwargs["ExpressionAttributeValues"][":status"] == "FAILED"
        assert call_kwargs["ExpressionAttributeValues"][":err"] == "bad input"

    def test_converts_exception_to_string(self, table):
        record_run_failed(table, "run-3", RuntimeError("boom"), "2025-01-15T12:00:00+00:00")

        call_kwargs = table.update_item.call_args[1]
        assert call_kwargs["ExpressionAttributeValues"][":err"] == "boom"

    def test_does_not_raise_on_failure(self, table):
        table.update_item.side_effect = Exception("DynamoDB down")
        record_run_failed(table, "run-3", "error", "2025-01-15T12:00:00+00:00")


# ── record_document ─────────────────────────────────────────────────


class TestRecordDocument:
    def test_writes_document_record_with_all_fields(self, table):
        record_document(
            table,
            run_id="run-4",
            tenant="tenanta",
            theme_name="VPN Issues",
            article_type="KB",
            article_uuid="abc-123",
            s3_key="tenanta/ic/2025-01-15/abc-123.json",
            short_description="How to fix VPN",
        )

        table.put_item.assert_called_once()
        item = table.put_item.call_args[1]["Item"]
        assert item["run_id"] == "run-4"
        assert item["record_type"] == "DOC#abc-123"
        assert item["tenant"] == "tenanta"
        assert item["theme_name"] == "VPN Issues"
        assert item["article_type"] == "KB"
        assert item["article_uuid"] == "abc-123"
        assert item["s3_key"] == "tenanta/ic/2025-01-15/abc-123.json"
        assert item["short_description"] == "How to fix VPN"
        assert "generation_timestamp" in item

    @patch("pipeline.app.metrics.datetime")
    def test_generation_timestamp_is_utc_iso(self, mock_dt, table):
        fixed = datetime(2025, 1, 15, 14, 30, 0, tzinfo=timezone.utc)
        mock_dt.now.return_value = fixed
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        record_document(
            table, "run-4", "t", "theme", "KB", "uuid-1", "key", "desc"
        )

        mock_dt.now.assert_called_once_with(timezone.utc)
        item = table.put_item.call_args[1]["Item"]
        assert item["generation_timestamp"] == fixed.isoformat()

    def test_does_not_raise_on_failure(self, table):
        table.put_item.side_effect = Exception("DynamoDB down")
        record_document(
            table, "run-4", "t", "theme", "KB", "uuid-1", "key", "desc"
        )
