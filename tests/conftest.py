import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "region-discovery"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "protection-check"))

import pytest
import boto3
from moto import mock_aws
from unittest.mock import MagicMock


@pytest.fixture
def aws_credentials():
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ["AWS_SECURITY_TOKEN"] = "testing"
    os.environ["AWS_SESSION_TOKEN"] = "testing"
    os.environ["AWS_DEFAULT_REGION"] = "eu-west-1"


@pytest.fixture
def mock_context():
    context = MagicMock()
    context.function_name = "test-function"
    context.memory_limit_in_mb = 128
    context.invoked_function_arn = "arn:aws:lambda:eu-west-1:111111111111:function:test-function"
    context.aws_request_id = "test-request-id"
    return context


@pytest.fixture
def organizations_setup(aws_credentials):
    """Create a moto Organizations org with a single account for tagging tests."""
    with mock_aws():
        client = boto3.client("organizations", region_name="us-east-1")
        client.create_organization(FeatureSet="ALL")
        accounts = client.list_accounts()
        account_id = accounts["Accounts"][0]["Id"]
        yield client, account_id