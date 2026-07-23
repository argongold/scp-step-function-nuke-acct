import os
import sys
import json
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "evaluation"))

import pytest
import boto3
from moto import mock_aws


@pytest.fixture
def base_event():
    return {
        "target_account_id": "123456789012",
        "execution_id": "arn:aws:states:eu-west-1:111111111111:execution:slz-account-teardown-orchestrator:123456789012",
    }


class TestEvaluationHandler:
    """Tests for the evaluation Lambda handler."""

    @mock_aws
    def test_all_regions_complete(self, aws_credentials, dynamodb_table, mock_context, base_event):
        """Should return all_complete=true when all regions are complete."""
        dynamodb_table.put_item(Item={
            "AccountId": "123456789012",
            "Region": "us-east-1",
            "ExecutionId": base_event["execution_id"],
            "RunCount": Decimal("1"),
            "Status": "complete",
            "RemainingResCount": Decimal("0"),
            "PreviousRemainingResCount": Decimal("10"),
            "RemovedCount": Decimal("10"),
            "Resources": "[]",
        })
        dynamodb_table.put_item(Item={
            "AccountId": "123456789012",
            "Region": "eu-west-1",
            "ExecutionId": base_event["execution_id"],
            "RunCount": Decimal("1"),
            "Status": "complete",
            "RemainingResCount": Decimal("0"),
            "PreviousRemainingResCount": Decimal("5"),
            "RemovedCount": Decimal("5"),
            "Resources": "[]",
        })

        from importlib import import_module
        evaluation = import_module("evaluation")

        result = evaluation.handler(base_event, mock_context)

        assert result["all_complete"] is True
        assert result["regions_remaining"] == []
        assert set(result["regions_complete"]) == {"us-east-1", "eu-west-1"}
        assert result["total_removed"] == 15

    @mock_aws
    def test_regions_remaining_with_progress(self, aws_credentials, dynamodb_table, mock_context, base_event):
        """Should return progress_detected=true when remaining count decreased."""
        dynamodb_table.put_item(Item={
            "AccountId": "123456789012",
            "Region": "us-east-1",
            "ExecutionId": base_event["execution_id"],
            "RunCount": Decimal("2"),
            "Status": "resources_remaining",
            "RemainingResCount": Decimal("5"),
            "PreviousRemainingResCount": Decimal("10"),
            "RemovedCount": Decimal("5"),
            "Resources": '["s3-bucket-xyz"]',
        })
        dynamodb_table.put_item(Item={
            "AccountId": "123456789012",
            "Region": "eu-west-1",
            "ExecutionId": base_event["execution_id"],
            "RunCount": Decimal("2"),
            "Status": "complete",
            "RemainingResCount": Decimal("0"),
            "PreviousRemainingResCount": Decimal("3"),
            "RemovedCount": Decimal("3"),
            "Resources": "[]",
        })

        from importlib import import_module
        evaluation = import_module("evaluation")

        result = evaluation.handler(base_event, mock_context)

        assert result["all_complete"] is False
        assert result["progress_detected"] is True
        assert result["regions_remaining"] == ["us-east-1"]
        assert result["stuck_regions"] == []
        assert result["summary"]["us-east-1"]["remaining_res_count"] == 5

    @mock_aws
    def test_stuck_regions_no_progress(self, aws_credentials, dynamodb_table, mock_context, base_event):
        """Should return progress_detected=false when no region made progress."""
        dynamodb_table.put_item(Item={
            "AccountId": "123456789012",
            "Region": "us-east-1",
            "ExecutionId": base_event["execution_id"],
            "RunCount": Decimal("3"),
            "Status": "resources_remaining",
            "RemainingResCount": Decimal("5"),
            "PreviousRemainingResCount": Decimal("5"),
            "RemovedCount": Decimal("0"),
            "Resources": '["s3-bucket-xyz"]',
        })

        from importlib import import_module
        evaluation = import_module("evaluation")

        result = evaluation.handler(base_event, mock_context)

        assert result["all_complete"] is False
        assert result["progress_detected"] is False
        assert result["stuck_regions"] == ["us-east-1"]

    @mock_aws
    def test_max_retries_reached(self, aws_credentials, dynamodb_table, mock_context, base_event):
        """Should return max_retries_reached=true when RunCount >= 5."""
        dynamodb_table.put_item(Item={
            "AccountId": "123456789012",
            "Region": "us-east-1",
            "ExecutionId": base_event["execution_id"],
            "RunCount": Decimal("5"),
            "Status": "resources_remaining",
            "RemainingResCount": Decimal("3"),
            "PreviousRemainingResCount": Decimal("5"),
            "RemovedCount": Decimal("2"),
            "Resources": "[]",
        })

        from importlib import import_module
        evaluation = import_module("evaluation")

        result = evaluation.handler(base_event, mock_context)

        assert result["max_retries_reached"] is True
        assert result["run_count"] == 5

    @mock_aws
    def test_error_region_always_retried(self, aws_credentials, dynamodb_table, mock_context, base_event):
        """Regions with remaining_res_count=-1 (error) should not be marked as stuck."""
        dynamodb_table.put_item(Item={
            "AccountId": "123456789012",
            "Region": "ap-southeast-1",
            "ExecutionId": base_event["execution_id"],
            "RunCount": Decimal("2"),
            "Status": "resources_remaining",
            "RemainingResCount": Decimal("-1"),
            "PreviousRemainingResCount": Decimal("-1"),
            "RemovedCount": Decimal("0"),
            "Resources": "[]",
        })

        from importlib import import_module
        evaluation = import_module("evaluation")

        result = evaluation.handler(base_event, mock_context)

        assert result["all_complete"] is False
        assert result["progress_detected"] is True
        assert result["stuck_regions"] == []
        assert "ap-southeast-1" in result["regions_remaining"]

    def test_handler_raises_on_missing_target_account_id(self, aws_credentials, mock_context):
        """Handler should raise KeyError if target_account_id is missing."""
        from importlib import import_module
        evaluation = import_module("evaluation")

        with pytest.raises(KeyError, match="target_account_id"):
            evaluation.handler({"execution_id": "test"}, mock_context)
