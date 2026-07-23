import os
import sys

import pytest
import boto3

import protection_check


class TestProtectionCheckHandler:
    """Tests for the protection check Lambda handler."""

    def test_returns_protected_when_tag_present(self, organizations_setup, mock_context):
        """Handler should return is_protected=true when core-protection=enabled tag exists."""
        client, account_id = organizations_setup

        client.tag_resource(ResourceId=account_id, Tags=[
            {"Key": "Environment", "Value": "sandbox"},
            {"Key": "core-protection", "Value": "enabled"},
            {"Key": "Owner", "Value": "platform-team"},
        ])

        result = protection_check.handler({"target_account_id": account_id}, mock_context)

        assert result["target_account_id"] == account_id
        assert result["is_protected"] is True

    def test_returns_not_protected_when_tag_absent(self, organizations_setup, mock_context):
        """Handler should return is_protected=false when core-protection tag does not exist."""
        client, account_id = organizations_setup

        client.tag_resource(ResourceId=account_id, Tags=[
            {"Key": "Environment", "Value": "sandbox"},
            {"Key": "Owner", "Value": "platform-team"},
        ])

        result = protection_check.handler({"target_account_id": account_id}, mock_context)

        assert result["target_account_id"] == account_id
        assert result["is_protected"] is False

    def test_returns_not_protected_when_tag_value_differs(self, organizations_setup, mock_context):
        """Handler should return is_protected=false when core-protection tag exists but value is not 'enabled'."""
        client, account_id = organizations_setup

        client.tag_resource(ResourceId=account_id, Tags=[
            {"Key": "core-protection", "Value": "disabled"},
        ])

        result = protection_check.handler({"target_account_id": account_id}, mock_context)

        assert result["target_account_id"] == account_id
        assert result["is_protected"] is False

    def test_returns_not_protected_when_no_tags(self, organizations_setup, mock_context):
        """Handler should return is_protected=false when account has no tags."""
        _, account_id = organizations_setup

        result = protection_check.handler({"target_account_id": account_id}, mock_context)

        assert result["target_account_id"] == account_id
        assert result["is_protected"] is False

    def test_handles_multiple_tags(self, organizations_setup, mock_context):
        """Handler should correctly find the protection tag among many tags."""
        client, account_id = organizations_setup

        client.tag_resource(ResourceId=account_id, Tags=[
            {"Key": f"tag-{i}", "Value": f"value-{i}"} for i in range(10)
        ])
        client.tag_resource(ResourceId=account_id, Tags=[
            {"Key": "core-protection", "Value": "enabled"},
        ])

        result = protection_check.handler({"target_account_id": account_id}, mock_context)

        assert result["is_protected"] is True

    def test_raises_on_missing_target_account_id(self, aws_credentials, mock_context):
        """Handler should raise KeyError if target_account_id is missing."""
        with pytest.raises(KeyError, match="target_account_id"):
            protection_check.handler({}, mock_context)
