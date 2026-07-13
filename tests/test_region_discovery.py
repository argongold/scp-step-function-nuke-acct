import os
import sys
import json
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "region-discovery"))

import pytest
import boto3
from moto import mock_aws


@pytest.fixture
def lambda_event():
    return {
        "target_account_id": "123456789012",
        "target_role_arn": "arn:aws:iam::123456789012:role/NukeExecutionRole",
    }


@pytest.fixture
def mock_context():
    context = MagicMock()
    context.function_name = "slz-account-teardown-region-discovery"
    context.memory_limit_in_mb = 128
    context.invoked_function_arn = "arn:aws:lambda:eu-west-1:111111111111:function:slz-account-teardown-region-discovery"
    context.aws_request_id = "test-request-id"
    return context


class TestRegionDiscoveryHandler:
    """Tests for the region discovery Lambda handler."""

    @mock_aws
    def test_handler_returns_enabled_regions(self, aws_credentials, lambda_event, mock_context):
        """Handler should return a list of enabled regions for the target account."""
        mock_regions = [
            {"RegionName": "us-east-1", "RegionOptStatus": "ENABLED_BY_DEFAULT"},
            {"RegionName": "eu-west-1", "RegionOptStatus": "ENABLED_BY_DEFAULT"},
            {"RegionName": "ap-southeast-1", "RegionOptStatus": "ENABLED"},
        ]

        mock_credentials = {
            "Credentials": {
                "AccessKeyId": "ASIA_FAKE_KEY",
                "SecretAccessKey": "fake_secret",
                "SessionToken": "fake_token",
                "Expiration": "2026-01-01T00:00:00Z",
            }
        }

        with patch("boto3.client") as mock_boto_client:
            # Mock STS client
            mock_sts = MagicMock()
            mock_sts.assume_role.return_value = mock_credentials

            # Mock Account client with paginator
            mock_account = MagicMock()
            mock_paginator = MagicMock()
            mock_paginator.paginate.return_value = [{"Regions": mock_regions}]
            mock_account.get_paginator.return_value = mock_paginator

            # Route boto3.client calls to the right mock
            def client_factory(service, **kwargs):
                if service == "sts":
                    return mock_sts
                elif service == "account":
                    return mock_account
                return MagicMock()

            mock_boto_client.side_effect = client_factory

            from importlib import import_module
            region_discovery = import_module("region-discovery")

            result = region_discovery.handler(lambda_event, mock_context)

        assert result["target_account_id"] == "123456789012"
        assert set(result["enabled_regions"]) == {"us-east-1", "eu-west-1", "ap-southeast-1"}
        assert len(result["enabled_regions"]) == 3

    @mock_aws
    def test_handler_assumes_correct_role(self, aws_credentials, lambda_event, mock_context):
        """Handler should assume the role specified in target_role_arn."""
        mock_credentials = {
            "Credentials": {
                "AccessKeyId": "ASIA_FAKE_KEY",
                "SecretAccessKey": "fake_secret",
                "SessionToken": "fake_token",
                "Expiration": "2026-01-01T00:00:00Z",
            }
        }

        with patch("boto3.client") as mock_boto_client:
            mock_sts = MagicMock()
            mock_sts.assume_role.return_value = mock_credentials

            mock_account = MagicMock()
            mock_paginator = MagicMock()
            mock_paginator.paginate.return_value = [{"Regions": []}]
            mock_account.get_paginator.return_value = mock_paginator

            def client_factory(service, **kwargs):
                if service == "sts":
                    return mock_sts
                elif service == "account":
                    return mock_account
                return MagicMock()

            mock_boto_client.side_effect = client_factory

            from importlib import import_module
            region_discovery = import_module("region-discovery")

            region_discovery.handler(lambda_event, mock_context)

        mock_sts.assume_role.assert_called_once_with(
            RoleArn="arn:aws:iam::123456789012:role/NukeExecutionRole",
            RoleSessionName="region-discovery",
        )

    @mock_aws
    def test_handler_uses_assumed_credentials_for_account_client(self, aws_credentials, lambda_event, mock_context):
        """Handler should create the account client with the assumed role credentials."""
        mock_credentials = {
            "Credentials": {
                "AccessKeyId": "ASIA_ASSUMED_KEY",
                "SecretAccessKey": "assumed_secret",
                "SessionToken": "assumed_token",
                "Expiration": "2026-01-01T00:00:00Z",
            }
        }

        with patch("boto3.client") as mock_boto_client:
            mock_sts = MagicMock()
            mock_sts.assume_role.return_value = mock_credentials

            mock_account = MagicMock()
            mock_paginator = MagicMock()
            mock_paginator.paginate.return_value = [{"Regions": []}]
            mock_account.get_paginator.return_value = mock_paginator

            def client_factory(service, **kwargs):
                if service == "sts":
                    return mock_sts
                elif service == "account":
                    return mock_account
                return MagicMock()

            mock_boto_client.side_effect = client_factory

            from importlib import import_module
            region_discovery = import_module("region-discovery")

            region_discovery.handler(lambda_event, mock_context)

        # Verify account client was created with assumed credentials
        mock_boto_client.assert_any_call(
            "account",
            aws_access_key_id="ASIA_ASSUMED_KEY",
            aws_secret_access_key="assumed_secret",
            aws_session_token="assumed_token",
        )

    @mock_aws
    def test_handler_returns_empty_list_when_no_regions(self, aws_credentials, lambda_event, mock_context):
        """Handler should return an empty list if no enabled regions are found."""
        mock_credentials = {
            "Credentials": {
                "AccessKeyId": "ASIA_FAKE_KEY",
                "SecretAccessKey": "fake_secret",
                "SessionToken": "fake_token",
                "Expiration": "2026-01-01T00:00:00Z",
            }
        }

        with patch("boto3.client") as mock_boto_client:
            mock_sts = MagicMock()
            mock_sts.assume_role.return_value = mock_credentials

            mock_account = MagicMock()
            mock_paginator = MagicMock()
            mock_paginator.paginate.return_value = [{"Regions": []}]
            mock_account.get_paginator.return_value = mock_paginator

            def client_factory(service, **kwargs):
                if service == "sts":
                    return mock_sts
                elif service == "account":
                    return mock_account
                return MagicMock()

            mock_boto_client.side_effect = client_factory

            from importlib import import_module
            region_discovery = import_module("region-discovery")

            result = region_discovery.handler(lambda_event, mock_context)

        assert result["target_account_id"] == "123456789012"
        assert result["enabled_regions"] == []

    def test_handler_raises_on_missing_target_account_id(self, aws_credentials, mock_context):
        """Handler should raise KeyError if target_account_id is missing."""
        from importlib import import_module
        region_discovery = import_module("region-discovery")

        with pytest.raises(KeyError, match="target_account_id"):
            region_discovery.handler({"target_role_arn": "arn:aws:iam::123456789012:role/NukeExecutionRole"}, mock_context)

    def test_handler_raises_on_missing_target_role_arn(self, aws_credentials, mock_context):
        """Handler should raise KeyError if target_role_arn is missing."""
        from importlib import import_module
        region_discovery = import_module("region-discovery")

        with pytest.raises(KeyError, match="target_role_arn"):
            region_discovery.handler({"target_account_id": "123456789012"}, mock_context)
