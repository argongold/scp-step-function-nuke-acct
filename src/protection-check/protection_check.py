import boto3
from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.typing import LambdaContext

logger = Logger(service="protection-check")

PROTECTION_TAG_KEY = "core-protection"
PROTECTION_TAG_VALUE = "enabled"


@logger.inject_lambda_context
def handler(event: dict, context: LambdaContext) -> dict:
    """Check if target account has core-protection tag enabled.

    Queries AWS Organizations for tags on the target account and checks
    whether the 'core-protection' tag is set to 'enabled'.

    Args:
        event: {
            "target_account_id": "123456789012"
        }

    Returns:
        {
            "target_account_id": "123456789012",
            "is_protected": true|false
        }
    """
    target_account_id = event["target_account_id"]

    logger.info("Checking protection tag for account", extra={
        "target_account_id": target_account_id,
    })

    orgs_client = boto3.client("organizations")

    # List all tags on the account resource
    tags = []
    paginator = orgs_client.get_paginator("list_tags_for_resource")
    for page in paginator.paginate(ResourceId=target_account_id):
        tags.extend(page["Tags"])

    # Check for core-protection=enabled
    is_protected = any(
        tag["Key"] == PROTECTION_TAG_KEY and tag["Value"] == PROTECTION_TAG_VALUE
        for tag in tags
    )

    logger.info("Protection check result", extra={
        "target_account_id": target_account_id,
        "is_protected": is_protected,
        "tags_found": len(tags),
    })

    return {
        "target_account_id": target_account_id,
        "is_protected": is_protected,
    }
