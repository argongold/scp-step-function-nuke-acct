import json
import os
from decimal import Decimal

import boto3
from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.typing import LambdaContext

logger = Logger(service="evaluation")

MAX_RETRIES = 5
TABLE_NAME = os.environ.get("STATE_TABLE_NAME", "slz-account-teardown-state-table")


@logger.inject_lambda_context
def handler(event: dict, context: LambdaContext) -> dict:
    """Evaluate nuke progress and determine next action.

    Queries DynamoDB for all region rows belonging to this execution,
    aggregates statuses, detects progress, and returns a decision object.

    Args:
        event: {
            "target_account_id": "123456789012",
            "execution_id": "<step-functions-execution-arn>"
        }

    Returns:
        Decision object for the Step Functions Choice state.
    """
    target_account_id = event["target_account_id"]
    execution_id = event["execution_id"]

    logger.info("Evaluating nuke progress", extra={
        "target_account_id": target_account_id,
        "execution_id": execution_id,
    })

    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(TABLE_NAME)

    # Query all region rows for this account
    response = table.query(
        KeyConditionExpression=boto3.dynamodb.conditions.Key("AccountId").eq(target_account_id),
        FilterExpression=boto3.dynamodb.conditions.Attr("ExecutionId").eq(execution_id),
    )
    items = response["Items"]

    # Categorize regions
    regions_complete = []
    regions_remaining = []
    stuck_regions = []
    total_removed = 0
    summary = {}
    run_count = 0

    for item in items:
        region = item["Region"]
        status = item["Status"]
        remaining_count = int(item.get("RemainingCount", 0))
        previous_remaining = int(item.get("PreviousRemainingCount", 0))
        removed_count = int(item.get("RemovedCount", 0))
        failed_resources = item.get("FailedResources", "[]")
        item_run_count = int(item.get("RunCount", 0))

        # Parse failed_resources if stored as JSON string
        if isinstance(failed_resources, str):
            try:
                failed_resources = json.loads(failed_resources)
            except (json.JSONDecodeError, TypeError):
                failed_resources = []

        run_count = max(run_count, item_run_count)
        total_removed += removed_count

        if status == "complete":
            regions_complete.append(region)
        else:
            regions_remaining.append(region)
            summary[region] = {
                "remaining_count": remaining_count,
                "failed_resources": failed_resources,
            }

            # Check if region is stuck (no progress between runs)
            # remaining_count == -1 means error/unknown — always retry
            if remaining_count >= 0 and previous_remaining > 0 and remaining_count >= previous_remaining:
                stuck_regions.append(region)

    all_complete = len(regions_remaining) == 0
    max_retries_reached = run_count >= MAX_RETRIES

    # Progress detected if at least one remaining region made progress
    # (or if it's the first run where previous_remaining is 0)
    progress_detected = len(stuck_regions) < len(regions_remaining) if regions_remaining else True

    result = {
        "all_complete": all_complete,
        "progress_detected": progress_detected,
        "run_count": run_count,
        "max_retries_reached": max_retries_reached,
        "regions_remaining": regions_remaining,
        "regions_complete": regions_complete,
        "total_removed": total_removed,
        "stuck_regions": stuck_regions,
        "summary": summary,
    }

    logger.info("Evaluation result", extra=result)

    return result
