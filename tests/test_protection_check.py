import os
import sys
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "protection-check"))

import pytest


@pytest.fixture
def lambda_event():
    return {
        "target_account_id": "123456789012",
    }


@pytest.fixture
def mock_context():
    context = MagicMock()
    context.function_name = "slz-account-teardown-protection-check"
    context.memory_limit_in_mb = 128
    context.invoked_function_arn = "arn:aws:lambda:eu-west-1:111111111111:function:slz-account-teardown-protection-check"
    context.aws_request_id = "test-request-id"
    return context


class TestProtectionCheckHandler:
    """Tests for the protection check Lambda handler."""

    def test_returns_protected_when_tag_present(self, aws_credentials, lambda_event, mock_context):
        """Handler should return is_protected=true when core-protection=enabled tag exists."""
        mock_tags = [
            {"Key": "Environment", "Value": "sandbox"},
            {"Key": "core-protection", "Value": "enabled"},
            {"Key": "Owner", "Value": "platform-team"},
        ]

        with patch("boto3.client") as mock_boto_client:
            mock_orgs = MagicMock()
            mock_paginator = MagicMock()
            mock_paginator.paginate.return_value = [{"Tags": mock_tags}]
            mock_orgs.get_paginator.return_value = mock_paginator
            mock_boto_client.return_value = mock_orgs

            import protection_check

            result = protection_check.handler(lambda_event, mock_context)

        assert result["target_account_id"] == "123456789012"
        assert result["is_protected"] is True

    def test_returns_not_protected_when_tag_absent(self, aws_credentials, lambda_event, mock_context):
        """Handler should return is_protected=false when core-protection tag does not exist."""
        mock_tags = [
            {"Key": "Environment", "Value": "sandbox"},
            {"Key": "Owner", "Value": "platform-team"},
        ]

        with patch("boto3.client") as mock_boto_client:
            mock_orgs = MagicMock()
            mock_paginator = MagicMock()
            mock_paginator.paginate.return_value = [{"Tags": mock_tags}]
            mock_orgs.get_paginator.return_value = mock_paginator
            mock_boto_client.return_value = mock_orgs

            import protection_check

            result = protection_check.handler(lambda_event, mock_context)

        assert result["target_account_id"] == "123456789012"
        assert result["is_protected"] is False

    def test_returns_not_protected_when_tag_value_differs(self, aws_credentials, lambda_event, mock_context):
        """Handler should return is_protected=false when core-protection tag exists but value is not 'enabled'."""
        mock_tags = [
            {"Key": "core-protection", "Value": "disabled"},
        ]

        with patch("boto3.client") as mock_boto_client:
            mock_orgs = MagicMock()
            mock_paginator = MagicMock()
            mock_paginator.paginate.return_value = [{"Tags": mock_tags}]
            mock_orgs.get_paginator.return_value = mock_paginator
            mock_boto_client.return_value = mock_orgs

            import protection_check

            result = protection_check.handler(lambda_event, mock_context)

        assert result["target_account_id"] == "123456789012"
        assert result["is_protected"] is False

    def test_returns_not_protected_when_no_tags(self, aws_credentials, lambda_event, mock_context):
        """Handler should return is_protected=false when account has no tags."""
        with patch("boto3.client") as mock_boto_client:
            mock_orgs = MagicMock()
            mock_paginator = MagicMock()
            mock_paginator.paginate.return_value = [{"Tags": []}]
            mock_orgs.get_paginator.return_value = mock_paginator
            mock_boto_client.return_value = mock_orgs

            import protection_check

            result = protection_check.handler(lambda_event, mock_context)

        assert result["target_account_id"] == "123456789012"
        assert result["is_protected"] is False

    def test_handles_paginated_tags(self, aws_credentials, lambda_event, mock_context):
        """Handler should handle multiple pages of tags."""
        page1 = {"Tags": [{"Key": "Environment", "Value": "sandbox"}]}
        page2 = {"Tags": [{"Key": "core-protection", "Value": "enabled"}]}

        with patch("boto3.client") as mock_boto_client:
            mock_orgs = MagicMock()
            mock_paginator = MagicMock()
            mock_paginator.paginate.return_value = [page1, page2]
            mock_orgs.get_paginator.return_value = mock_paginator
            mock_boto_client.return_value = mock_orgs

            import protection_check

            result = protection_check.handler(lambda_event, mock_context)

        assert result["is_protected"] is True

    def test_calls_list_tags_with_correct_resource_id(self, aws_credentials, lambda_event, mock_context):
        """Handler should call ListTagsForResource with the target account ID."""
        with patch("boto3.client") as mock_boto_client:
            mock_orgs = MagicMock()
            mock_paginator = MagicMock()
            mock_paginator.paginate.return_value = [{"Tags": []}]
            mock_orgs.get_paginator.return_value = mock_paginator
            mock_boto_client.return_value = mock_orgs

            import protection_check

            protection_check.handler(lambda_event, mock_context)

        mock_orgs.get_paginator.assert_called_once_with("list_tags_for_resource")
        mock_paginator.paginate.assert_called_once_with(ResourceId="123456789012")

    def test_raises_on_missing_target_account_id(self, aws_credentials, mock_context):
        """Handler should raise KeyError if target_account_id is missing."""
        import protection_check

        with pytest.raises(KeyError, match="target_account_id"):
            protection_check.handler({}, mock_context)
