import json

import boto3
from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.typing import LambdaContext

logger = Logger(service="region-discovery")


@logger.inject_lambda_context
def handler(event: dict, context: LambdaContext) -> dict:
    """Discover enabled regions in a target AWS account.

    Assumes NukeExecutionRole in the target account and calls
    account:ListRegions to retrieve all enabled regions.

    Args:
        event: {
            "target_account_id": "123456789012",
            "target_role_arn": "arn:aws:iam::123456789012:role/NukeExecutionRole"
        }

    Returns:
        {
            "target_account_id": "123456789012",
            "enabled_regions": ["us-east-1", "eu-west-1", ...]
        }
    """
    target_account_id = event["target_account_id"]
    target_role_arn = event["target_role_arn"]

    logger.info("Assuming role in target account", extra={
        "target_account_id": target_account_id,
        "target_role_arn": target_role_arn,
    })

    # Assume NukeExecutionRole in the target account
    sts_client = boto3.client("sts")
    credentials = sts_client.assume_role(
        RoleArn=target_role_arn,
        RoleSessionName="region-discovery",
    )["Credentials"]

    # Create account client with assumed role credentials
    account_client = boto3.client(
        "account",
        aws_access_key_id=credentials["AccessKeyId"],
        aws_secret_access_key=credentials["SecretAccessKey"],
        aws_session_token=credentials["SessionToken"],
    )

    # List all enabled regions
    enabled_regions = []
    paginator = account_client.get_paginator("list_regions")
    for page in paginator.paginate(RegionOptStatusContains=["ENABLED", "ENABLED_BY_DEFAULT"]):
        for region in page["Regions"]:
            enabled_regions.append(region["RegionName"])

    logger.info("Discovered enabled regions", extra={
        "target_account_id": target_account_id,
        "region_count": len(enabled_regions),
        "enabled_regions": enabled_regions,
    })

    return {
        "target_account_id": target_account_id,
        "enabled_regions": enabled_regions,
    }
