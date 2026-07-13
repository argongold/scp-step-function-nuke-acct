import os
import sys
import json
from unittest.mock import MagicMock, patch
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "evaluation"))

import pytest
import boto3
from moto import mock_aws


@pytest.fixture
def dynamodb_table(aws_credentials):
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="eu-west-1")
        table = dynamodb.create_table(
            TableName="slz-account-teardown-state-table",
            KeySchema=[
                {"AttributeName": "AccountId", "KeyType": "HASH"},
                {"AttributeName": "Region", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "AccountId", "AttributeType": "S"},
                {"AttributeName": "Region", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table.meta.client.get_waiter("table_exists").wait(TableName="slz-account-teardown-state-table")
        yield table


@pytest.fixture
def mock_context():
    context = MagicMock()
    context.function_name = "slz-account-teardown-evaluation"
    context.memory_limit_in_mb = 128
    context.invoked_function_arn = "arn:aws:lambda:eu-west-1:111111111111:function:slz-account-teardown-evaluation"
    context.aws_request_id = "test-request-id"
    return context


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
            "RemainingCount": Decimal("0"),
            "PreviousRemainingCount": Decimal("10"),
            "RemovedCount": Decimal("10"),
            "FailedResources": "[]",
        })
        dynamodb_table.put_item(Item={
            "AccountId": "123456789012",
            "Region": "eu-west-1",
            "ExecutionId": base_event["execution_id"],
            "RunCount": Decimal("1"),
            "Status": "complete",
            "RemainingCount": Decimal("0"),
            "PreviousRemainingCount": Decimal("5"),
            "RemovedCount": Decimal("5"),
            "FailedResources": "[]",
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
            "RemainingCount": Decimal("5"),
            "PreviousRemainingCount": Decimal("10"),
            "RemovedCount": Decimal("5"),
            "FailedResources": '["s3-bucket-xyz"]',
        })
        dynamodb_table.put_item(Item={
            "AccountId": "123456789012",
            "Region": "eu-west-1",
            "ExecutionId": base_event["execution_id"],
            "RunCount": Decimal("2"),
            "Status": "complete",
            "RemainingCount": Decimal("0"),
            "PreviousRemainingCount": Decimal("3"),
            "RemovedCount": Decimal("3"),
            "FailedResources": "[]",
        })

        from importlib import import_module
        evaluation = import_module("evaluation")

        result = evaluation.handler(base_event, mock_context)

        assert result["all_complete"] is False
        assert result["progress_detected"] is True
        assert result["regions_remaining"] == ["us-east-1"]
        assert result["stuck_regions"] == []
        assert result["summary"]["us-east-1"]["remaining_count"] == 5

    @mock_aws
    def test_stuck_regions_no_progress(self, aws_credentials, dynamodb_table, mock_context, base_event):
        """Should return progress_detected=false when no region made progress."""
        dynamodb_table.put_item(Item={
            "AccountId": "123456789012",
            "Region": "us-east-1",
            "ExecutionId": base_event["execution_id"],
            "RunCount": Decimal("3"),
            "Status": "resources_remaining",
            "RemainingCount": Decimal("5"),
            "PreviousRemainingCount": Decimal("5"),
            "RemovedCount": Decimal("0"),
            "FailedResources": '["s3-bucket-xyz"]',
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
            "RemainingCount": Decimal("3"),
            "PreviousRemainingCount": Decimal("5"),
            "RemovedCount": Decimal("2"),
            "FailedResources": "[]",
        })

        from importlib import import_module
        evaluation = import_module("evaluation")

        result = evaluation.handler(base_event, mock_context)

        assert result["max_retries_reached"] is True
        assert result["run_count"] == 5

    @mock_aws
    def test_error_region_always_retried(self, aws_credentials, dynamodb_table, mock_context, base_event):
        """Regions with remaining_count=-1 (error) should not be marked as stuck."""
        dynamodb_table.put_item(Item={
            "AccountId": "123456789012",
            "Region": "ap-southeast-1",
            "ExecutionId": base_event["execution_id"],
            "RunCount": Decimal("2"),
            "Status": "resources_remaining",
            "RemainingCount": Decimal("-1"),
            "PreviousRemainingCount": Decimal("-1"),
            "RemovedCount": Decimal("0"),
            "FailedResources": "[]",
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
